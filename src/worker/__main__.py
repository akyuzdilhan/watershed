"""Run the worker on a replay file, or on the Service Bus subscription.

Usage:
    python -m worker data/replay/demo-0007.jsonl.gz --truth data/replay/demo-0007.truth.json   # offline, scored against the ground truth
    python -m worker --live --cosmos                                                          # Service Bus in, Cosmos DB out
Needs SERVICE_BUS_FQDN for --live and COSMOS_ENDPOINT for --cosmos (from the environment, .env or the azd environment).
"""

import argparse
import json
import logging
from pathlib import Path

from generator.publish import load, service_bus_settings
from worker.pipeline import MemorySink, Pipeline
from worker.spotter import score_against_truth


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("replay", type=Path, nargs="?", help="replay file (.jsonl or .jsonl.gz)")
    parser.add_argument("--truth", type=Path, help="ground truth of the replay, to score the cards against it")
    parser.add_argument("--live", action="store_true", help="read the Service Bus subscription instead of a file")
    parser.add_argument("--cosmos", action="store_true", help="write events, cards and the audit trail to Cosmos DB")
    parser.add_argument("--writer", choices=("none", "template", "foundry"), default="none", help="write the cards: fixed templates, or the Writer agent on Foundry")
    parser.add_argument("--auto-approve", action="store_true", help="put every written card on air without a producer (judging mode)")
    parser.add_argument("--threshold", type=float, default=0.08, help="minimum value of a chance that is not a goal, as a share of the result probabilities")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    for noisy in ("azure", "agent_framework", "httpx"):  # the SDKs log every HTTP request, AMQP link change and tool call at INFO
        logging.getLogger(noisy).setLevel(logging.WARNING)

    sink = MemorySink()
    if args.cosmos:
        from worker.azure_io import cosmos_from_env

        service_bus_settings()  # loads .env and the azd environment
        sink = cosmos_from_env()
    desk = None
    if args.writer != "none":
        from worker.desk import Desk
        from worker.writer import FoundryWriter, TemplateWriter

        if args.writer == "foundry":
            service_bus_settings()  # loads .env (Foundry endpoint and model)
        desk = Desk(sink, FoundryWriter() if args.writer == "foundry" else TemplateWriter(), auto_approve=args.auto_approve)
    pipeline = Pipeline(sink=sink, desk=desk, threshold=args.threshold)

    if args.live:
        from worker.azure_io import listen

        fqdn, topic = service_bus_settings()
        listen(pipeline, fqdn, topic)
        return
    if args.replay is None:
        parser.error("give a replay file, or --live")
    results = [r for m in load(args.replay) for r in pipeline.handle(m)] + pipeline.flush()
    for r in results:
        if "action" in r:
            print(f"  {r['minute_label']:>7}  {r['action']:<8} {r['card_id']}  ({r['cause']})")
        else:
            facts = r["facts"]
            print(f"  {r['minute_label']:>7}  card     {r['moment']:<9} {r['team_name']:<7} {r['player']['name']:<22} value {r['value_pts']:>3} pts  win {facts['team_wp_before_pct']}%")
            for t in r.get("texts", []):
                print(f"           [{t['lang']}] {t['headline']}: {t['why']}")
            if "texts" in r:
                print(f"           {r['writer']}, verified {r['verification']['verified']}, {r['status']}")
    match = next(iter(pipeline.matches.values()))
    print(f"{len(match.messages)} messages, {match.duplicates} duplicates dropped, {match.late} processed late; final score {match.score()}")
    if args.truth and isinstance(sink, MemorySink):
        spotter = next(iter(pipeline.spotters.values()))
        report = score_against_truth(spotter.cards, sink.audit, json.loads(args.truth.read_text(encoding="utf-8")))
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
