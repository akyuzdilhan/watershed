"""Run the worker on a replay file, or on the Service Bus subscription.

Usage:
    python -m worker data/replay/demo-0007.jsonl.gz --truth data/replay/demo-0007.truth.json   # offline, scored against the ground truth
    python -m worker --live --cosmos                                                          # Service Bus in, Cosmos DB out
    python -m worker --live --cosmos --forever                                                # keep listening for new matches (container)
    python -m worker data/replay/demo-0007.jsonl.gz --history data/history/1                  # with the insight engine, against the league history
    python -m worker data/replay/demo-0007.jsonl.gz --writer foundry --facts mcp              # the Writer reads its facts through the desk's MCP server
    python -m worker data/replay/demo-0007.jsonl.gz --writer template --duo agents --duo-dry --history data/history/9 --league-seed 9   # the duo by two agents, offline
Needs SERVICE_BUS_FQDN for --live and COSMOS_ENDPOINT for --cosmos (from the environment, .env or the azd environment). With APPLICATIONINSIGHTS_CONNECTION_STRING set, the traces go to Application Insights (worker.telemetry).

With a writer, the desk writes the cards and their duo with it, the insights and the storyline with the templates (unless --insights-writer writer), and at the final whistle the post-match recap (unless --no-recap): with --writer foundry, the recap agent on Foundry. Every model call of a match comes out of one budget of 40 (worker.desk); the duo by agents (--duo agents, worker.agent_desk) has its own, --duo-calls.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from generator.publish import load, service_bus_settings
from worker import telemetry
from worker.pipeline import MemorySink, Pipeline, ThreadedSink
from worker.spotter import score_against_truth


def load_history(path: Path):
    """The league history at `path`, through whatever loader `league.history` offers."""
    from league import history as module

    for name in ("load", "load_history", "open_history"):
        if callable(getattr(module, name, None)):
            return getattr(module, name)(path)
    cls = module.History
    for name in ("load", "from_dir", "open"):
        if callable(getattr(cls, name, None)):
            return getattr(cls, name)(path)
    return cls(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("replay", type=Path, nargs="?", help="replay file (.jsonl or .jsonl.gz)")
    parser.add_argument("--truth", type=Path, help="ground truth of the replay, to score the cards against it")
    parser.add_argument("--live", action="store_true", help="read the Service Bus subscription instead of a file")
    parser.add_argument("--cosmos", action="store_true", help="write events, cards and the audit trail to Cosmos DB")
    parser.add_argument("--desk-dir", type=Path, help="write cards, match state and audit as JSON files here (what the console reads without Cosmos DB)")
    parser.add_argument("--writer", choices=("none", "template", "foundry"), default="none", help="write the cards: fixed templates, or the Writer agent on Foundry")
    parser.add_argument("--insights-writer", choices=("template", "writer"), default="template",
                        help="who writes the insights and the storyline: the templates (default: the model only writes the cards and the recap), or the --writer")
    parser.add_argument("--no-recap", action="store_true", help="do not write the post-match recap at the final whistle")
    parser.add_argument("--auto-approve", action="store_true", help="put every written card on air without a producer (judging mode)")
    parser.add_argument("--threshold", type=float, default=0.08, help="minimum value of a chance that is not a goal, as a share of the result probabilities")
    parser.add_argument("--forever", action="store_true", help="with --live: keep waiting for new sessions instead of stopping when the subscription is idle")
    parser.add_argument("--idle", type=float, default=30.0, help="with --live: seconds without a message before a session is released (and, without --forever, the worker stops)")
    parser.add_argument("--workers", type=int, default=8, help="with --cosmos: concurrent event writes")
    parser.add_argument("--history", type=Path, help="league history (data/history/<seed>/): surface insights against it")
    parser.add_argument("--league-seed", type=int, help="league season of the match (e.g. 9): write the pre-match storyline and the season context")
    parser.add_argument("--matchday", type=int, default=30, help="matchday of the match in that season")
    parser.add_argument("--facts", choices=("local", "mcp"), default="local", help="with --writer foundry: the Writer reads get_moment from the desk's MCP server, in process")
    parser.add_argument("--duo", choices=("writer", "agents"), default="writer",
                        help="commentary duo: from the Writer call or the templates (writer), or from two agents, Wren and Joss, after the card (agents)")
    parser.add_argument("--duo-dry", action="store_true", help="with --duo agents: an offline scripted model for Wren and Joss (no Azure, no cost)")
    parser.add_argument("--duo-calls", type=int, default=36, help="with --duo agents: agent runs per match for the duo, apart from the Writer's 40")
    parser.add_argument("--duo-min-value", type=int, default=10, help="with --duo agents: minimum card value (value_pts) for the agents' duo")
    parser.add_argument("--duo-deadline", type=float, default=30.0, help="with --duo agents: seconds for one duo from its start; beyond, the card keeps its template duo")
    args = parser.parse_args()
    if args.duo == "agents" and args.writer == "none":
        parser.error("--duo agents needs --writer template or --writer foundry")
    if args.duo_dry and args.duo != "agents":
        parser.error("--duo-dry goes with --duo agents")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    for noisy in ("azure", "agent_framework", "httpx", "mcp", "agent_framework_orchestrations"):  # the SDKs log every HTTP request, AMQP link change and tool call at INFO
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.facts == "mcp" and args.writer != "foundry":
        logging.warning("--facts mcp only changes --writer foundry")

    sink = MemorySink()
    if args.desk_dir:
        from worker.pipeline import JsonFileSink

        sink = JsonFileSink(args.desk_dir)
    if args.cosmos:
        from worker.azure_io import cosmos_from_env

        service_bus_settings()  # loads .env and the azd environment
        sink = ThreadedSink(cosmos_from_env(), workers=args.workers)
    history = load_history(args.history) if args.history else None
    desk = None
    history_facts = None
    if args.writer != "none":
        from worker.desk import Desk
        from worker.writer import FoundryWriter, TemplateWriter

        writer, recap_writer = TemplateWriter(), None
        if args.writer == "foundry":
            from worker.recap import RecapWriter

            service_bus_settings()  # loads .env (Foundry endpoint and model)
            writer, recap_writer = FoundryWriter(), RecapWriter()
        registry = None
        if args.facts == "mcp" or args.duo == "agents":
            from worker.mcp_server import FactRegistry

            registry = FactRegistry()  # what the desk's MCP server serves
        if args.writer == "foundry" and args.facts == "mcp":
            from worker.mcp_writer import McpWriter

            writer = McpWriter(registry)
        options = {"auto_approve": args.auto_approve, "recap_writer": recap_writer, "insight_writer": TemplateWriter() if args.insights_writer == "template" else None}
        if args.duo == "agents":
            from worker.agent_desk import AgentDesk
            from worker.duo_agents import DuoModel, DuoRunner, duo_summary
            from worker.mcp_server import HistoryFacts

            if not args.duo_dry:
                service_bus_settings()  # loads .env (Foundry endpoint and the duo's model)
            history_facts = HistoryFacts.build(history) if history is not None else None
            runner = DuoRunner(DuoModel(dry=args.duo_dry), registry, history_facts, deadline_s=args.duo_deadline, stale_after_s=20.0 if args.live else None,
                               max_calls_per_match=args.duo_calls)
            desk = AgentDesk(sink, writer, runner=runner, registry=registry, min_value=args.duo_min_value, **options)
        else:
            desk = Desk(sink, writer, **options)
    telemetry.configure()  # after the .env, when one is read above; nothing without APPLICATIONINSIGHTS_CONNECTION_STRING
    storyline_for = None
    if args.league_seed is not None:
        from worker.storyline import build_storyline

        def storyline_for(match_id: str, teams: dict) -> dict:
            return build_storyline(args.league_seed, args.matchday, history, match_id, home=teams["home"], away=teams["away"])
    shared = {"_history_view": history_facts.view} if history_facts is not None else {}  # the insight engine and the history tools read one view
    pipeline = Pipeline(sink=sink, desk=desk, threshold=args.threshold, history=history, storyline_for=storyline_for, recap=not args.no_recap, **shared)
    close_desk = getattr(desk, "close", None)  # a desk with work in the background (--duo agents)

    if args.live:
        from worker.azure_io import listen

        fqdn, topic = service_bus_settings()
        listen(pipeline, fqdn, topic, idle_s=args.idle, forever=args.forever)
        if close_desk is not None:
            close_desk(20.0)
        if isinstance(sink, ThreadedSink):
            sink.close()
        return
    if args.replay is None:
        parser.error("give a replay file, or --live")
    results = [r for m in load(args.replay) for r in pipeline.handle(m)] + pipeline.flush()
    if close_desk is not None:
        close_desk(None)
    if isinstance(sink, ThreadedSink):
        errors = sink.close()
        if errors:
            print(f"{len(errors)} writes failed, first: {errors[0]!r}", file=sys.stderr)
    for r in results:
        if "action" in r:
            print(f"  {r['minute_label']:>7}  {r['action']:<8} {r['card_id']}  ({r['cause']})")
        else:
            facts = r["facts"]
            print(f"  {r['minute_label']:>7}  card     {r['moment']:<9} {r['team_name']:<7} {r['player']['name']:<22} rank value {r['value_pts']:>3}  win {facts['team_wp_before_pct']}%")
            for t in r.get("texts", []):
                print(f"           [{t['lang']}] {t['headline']}: {t['why']}")
            if "texts" in r:
                print(f"           {r['writer']}, verified {r['verification']['verified']}, {r['status']}")
                if args.duo == "agents":
                    print(f"           duo {r['duo_writer']}: {duo_summary(r)}")
    match = next(iter(pipeline.matches.values()))
    for doc in pipeline.insights(match.match_id):
        mark = "*" if doc.get("stat_of_the_match") else " "
        print(f" {mark}{doc['minute_label'] or '':>7}  insight  {doc['key']:<34} {doc['status']:<10} rank value {doc['value']:>3}  {doc['rarity']['basis']}")
    recap = pipeline.recaps.get(match.match_id)
    if recap:
        print(f"  {'':>7}  recap    {recap['writer']}, verified {recap['verification']['verified']}")
        for t in recap["texts"]:
            print(f"           [{t['lang']}] {t['recap']}")
    print(f"{len(match.messages)} messages, {match.duplicates} duplicates dropped, {match.late} processed late; final score {match.score()}")
    transports = getattr(getattr(desk, "writer", None), "transports", None)
    if transports:
        print(f"Writer facts: {transports['mcp']} through MCP, {transports['local']} local")
    if args.duo == "agents":
        print(f"duo agents ({desk.runner.model.name}): {desk.applied} written, {dict(desk.fallbacks)} template, "
              f"{desk.runner.budget.used(match.match_id)} of {args.duo_calls} runs")
    if args.truth and isinstance(sink, MemorySink):
        spotter = next(iter(pipeline.spotters.values()))
        report = score_against_truth(spotter.cards, sink.audit, json.loads(args.truth.read_text(encoding="utf-8")))
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
