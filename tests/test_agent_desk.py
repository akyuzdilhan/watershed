"""The desk with the duo by two agents (worker.agent_desk): the card first with its template duo, the agents' duo after, the producer's decisions kept, the budget, and the pipeline's poll. Scripted or dry chat clients only: no model, no network."""

import asyncio
import copy
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from generator import generate_match
from league.history import generate
from scripted_chat import ScriptedModel, call, turn
from worker.agent_desk import AgentDesk, TextsOnly
from worker.desk import Desk
from worker.duo_agents import DuoModel, DuoRunner
from worker.mcp_server import FactRegistry, HistoryFacts
from worker.pipeline import MemorySink, Pipeline
from worker.writer import TemplateWriter, card_facts, draft, low_stake, verify_duo

LANGS = ["en", "fr", "es"]
TEAMS = {"home": "Varleigh", "away": "Yarnford"}
GOALS_FIELD = "player_marijn_scarpetta_league_goals"
SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(scope="module")
def history():
    return generate(9, 5)


@pytest.fixture(scope="module")
def hf(history):
    return HistoryFacts.build(history)


def goal_card(**extra) -> dict:
    """Varleigh go ahead at 66' through Marijn Scarpetta, from 30% to 54% (value 24): a two-turn duo."""
    card = {"id": "card-g66", "version": 1, "match_id": "m", "event_id": "g66", "team": "home", "team_name": "Varleigh", "moment": "goal", "minute_label": "66'",
            "clock_s": 3960.0, "player": {"id": "home-20", "name": "Marijn Scarpetta"}, "score_before": {"home": 1, "away": 1},
            "facts": {"team_wp_before_pct": 30, "team_wp_after_pct": 54}, "value_pts": 24}
    return card | extra


def react():
    return turn(("en", "Varleigh are in front, and this place is shaking!"), ("fr", "Varleigh passe devant, et tout le stade tremble !"),
                ("es", "¡El Varleigh se pone por delante y el estadio tiembla!"))


def card_detail():
    c = [{"value": 30, "source_field": "varleigh_wp_before_pct"}, {"value": 54, "source_field": "varleigh_wp_after_pct"}, {"value": 24, "source_field": "swing_pts"}]
    return turn(("en", "Varleigh go from 30% to 54% to win, up 24 points.", c), ("fr", "Varleigh passe de 30 % à 54 % de chances de gagner, soit 24 points de mieux.", c),
                ("es", "El Varleigh pasa del 30% al 54% de opciones de ganar: 24 puntos más.", c))


def history_detail(goals):
    c = [{"value": goals, "source_field": GOALS_FIELD}]
    return turn(("en", f"Marijn Scarpetta had {goals} league goals before this season.", c), ("fr", f"Marijn Scarpetta avait {goals} buts en championnat avant cette saison.", c),
                ("es", f"Marijn Scarpetta tenía {goals} goles de liga antes de esta temporada.", c))


def desk_with(model, hf, **kwargs):
    registry = FactRegistry()
    runner = DuoRunner(model, registry, hf, max_calls_per_match=kwargs.pop("duo_calls", 36))
    sink = kwargs.pop("sink", None) or MemorySink()
    writer = kwargs.pop("writer", None) or TemplateWriter()
    return AgentDesk(sink, writer, runner=runner, registry=registry, **kwargs), sink


def actions(sink, card_id):
    return [e["action"] for e in sink.audit if e.get("card_id") == card_id]


# --- the card first, the agents' duo after --------------------------------------------------------------

def test_the_card_does_not_wait_for_the_duo(hf):
    goals = hf.player("Marijn Scarpetta")[0][GOALS_FIELD]
    speaking, go_on = threading.Event(), threading.Event()

    def wren(messages):  # Wren's first request holds the duo's thread until the card is checked
        speaking.set()
        go_on.wait(10)
        return react()

    model = ScriptedModel([wren], [call("player_history", name="Marijn Scarpetta"), history_detail(goals)])
    desk, sink = desk_with(model, hf, auto_approve=True)
    card = goal_card()
    desk.write(card, TEAMS)
    assert speaking.wait(10) and not desk.runner.wait(0)  # write has returned while the job is still running
    stored = sink.cards[card["id"]]
    assert stored["duo_writer"] == "template" and stored["status"] == "approved" and stored["duo"]
    go_on.set()
    desk.close()
    stored = sink.cards[card["id"]]
    assert stored["duo_writer"] == "agents:scripted" and stored["duo_trace"]["applied"] is True and stored["duo_trace"]["reason"] is None
    assert [h["action"] for h in stored["history"]] == ["written", "approved", "duo_written"]
    entry = next(e for e in sink.audit if e["action"] == "duo_written")
    assert entry["by"] == "agents:scripted" and (entry["turns"], entry["runs"]) == (2, 2) and entry["tools"] == ["player_history(Marijn Scarpetta)"]
    assert entry["cause"].startswith("wren, joss (player_history): 2 runs, ")
    assert desk.applied == 1 and not desk.fallbacks


def test_the_agents_duo_replaces_the_template_once_verified(hf):
    goals = hf.player("Marijn Scarpetta")[0][GOALS_FIELD]
    model = ScriptedModel([react()], [call("player_history", name="Marijn Scarpetta"), history_detail(goals)])
    desk, sink = desk_with(model, hf, auto_approve=True)
    card = goal_card()
    desk.write(card, TEAMS)
    template = copy.deepcopy(card["duo"])
    desk.close()
    stored = sink.cards[card["id"]]
    assert stored["duo"] != template and [l["speaker"] for l in stored["duo"] if l["lang"] == "en"] == ["wren", "joss"]
    tool_facts = stored["duo_trace"]["tool_facts"]
    assert tool_facts[GOALS_FIELD]["value"] == goals
    assert verify_duo(stored["duo"], LANGS, card_facts(stored, TEAMS)[0] | {k: v["value"] for k, v in tool_facts.items()})["verified"]
    remembered = [text for lang, kind, text in desk.memory("m").docs[card["id"]] if kind == "duo"]
    assert remembered == [l["line"] for l in stored["duo"]]
    assert stored["texts"] == card["texts"]  # the texts never change


def test_a_refused_duo_leaves_the_template_on_the_card(hf):
    invented = turn(("en", "Even Marco Pellegrini would envy Varleigh tonight."), ("fr", "Même Marco Pellegrini envierait Varleigh ce soir."),
                    ("es", "Hasta Marco Pellegrini envidiaría al Varleigh esta noche."))
    desk, sink = desk_with(ScriptedModel([react()], [invented, invented]), hf, auto_approve=True)
    card = goal_card()
    desk.write(card, TEAMS)
    template = copy.deepcopy(card["duo"])
    desk.close()
    stored = sink.cards[card["id"]]
    assert stored["duo"] == template and stored["duo_writer"] == "template"
    assert stored["duo_trace"]["applied"] is False and stored["duo_trace"]["reason"] == "names"
    assert [(e["reason"], e["by"]) for e in sink.audit if e["action"] == "duo_fallback"] == [("names", "agents:scripted")]
    assert desk.fallbacks == {"names": 1} and desk.applied == 0


def test_the_producers_decisions_survive_the_duo(hf):
    model = ScriptedModel([react()], [card_detail()], delay=0.2)
    desk, sink = desk_with(model, hf)
    card = goal_card()
    desk.write(card, TEAMS)
    stored = sink.cards[card["id"]]
    assert stored["status"] == "pending"
    edited = [dict(t, headline=t["headline"] + "!") for t in stored["texts"]]
    stored |= {"status": "approved", "texts": edited, "history": stored["history"] + [{"action": "approved", "by": "producer-1", "version": 1}]}  # what the console writes
    desk.close()
    final = sink.cards[card["id"]]
    assert final["status"] == "approved" and final["texts"] == edited and final["duo_writer"] == "agents:scripted"
    assert [h["action"] for h in final["history"]] == ["written", "approved", "duo_written"]


def test_a_retraction_keeps_the_template_and_a_rewrite_gets_its_own_duo(hf):
    model = ScriptedModel([react()] * 4, [card_detail()] * 4, delay=0.3)
    desk, sink = desk_with(model, hf, auto_approve=True)
    retracted, rewritten = goal_card(id="card-r", event_id="r"), goal_card(id="card-w", event_id="w", clock_s=4000.0, minute_label="67'")
    desk.write(retracted, TEAMS)
    template = copy.deepcopy(retracted["duo"])
    desk.on_action(retracted, {"action": "retract", "cause": "var_offside", "previous_status": "approved", "minute_label": "67'", "message_id": "x"}, TEAMS)
    ruling = copy.deepcopy(retracted["duo_ruling"])
    assert ruling
    desk.write(rewritten, TEAMS)
    rewritten |= {"version": 2, "player": {"id": "home-9", "name": "Elio Ingleby"}}
    desk.on_action(rewritten, {"action": "rewrite", "cause": "scorer_corrected"}, TEAMS)
    desk.close()
    r, w = sink.cards["card-r"], sink.cards["card-w"]
    assert r["duo"] == template and r["duo_writer"] == "template" and r["duo_ruling"] == ruling
    fallback = [e for e in sink.audit if e["card_id"] == "card-r" and e["action"] == "duo_fallback"]
    assert len(fallback) == 1 and fallback[0]["reason"] in ("retracted", "cancelled")
    assert w["version"] == 2 and w["duo_writer"] == "agents:scripted" and w["duo_trace"]["applied"]
    assert [e["version"] for e in sink.audit if e["card_id"] == "card-w" and e["action"] in ("duo_written", "duo_fallback")] == [2]


def test_a_rewrite_never_keeps_the_trace_of_the_version_before(hf):
    model = ScriptedModel([react(), react()], [card_detail(), card_detail()], delay=0.3)
    desk, sink = desk_with(model, hf, auto_approve=True)
    card = goal_card()
    desk.write(card, TEAMS)
    assert desk.runner.wait(10)
    desk.poll()
    assert card["duo_writer"] == "agents:scripted" and card["duo_trace"]["applied"]
    card |= {"version": 2, "player": {"id": "home-9", "name": "Elio Ingleby"}}
    desk.on_action(card, {"action": "rewrite", "cause": "scorer_corrected"}, TEAMS)
    assert sink.cards[card["id"]]["duo_writer"] == "template" and "duo_trace" not in sink.cards[card["id"]]
    desk.close()
    stored = sink.cards[card["id"]]
    assert stored["version"] == 2 and stored["duo_writer"] == "agents:scripted" and stored["duo_trace"]["applied"]
    assert [e["version"] for e in sink.audit if e["action"] == "duo_written"] == [1, 2]


# --- the budget -------------------------------------------------------------------------------------------

class FakeWriter:
    """Writes the template texts as if a model had, and records what each call was asked."""

    name = "fake"

    def __init__(self) -> None:
        self.calls = []

    async def write_async(self, card, facts, languages, note=None, avoid=None, duo=False):
        self.calls.append({"card": card["id"], "duo": duo, "kwargs": {k: v for k, v in (("note", note), ("avoid", avoid)) if v is not None}})
        if card.get("type") in ("insight", "storyline"):
            return TemplateWriter().write_insight(card, facts[0], languages, duo=duo)
        return TemplateWriter().write(card, facts[0], languages, duo=duo)


def test_the_duo_has_a_budget_of_its_own(hf):
    model = ScriptedModel([react()] * 2, [card_detail()] * 2)
    writer = FakeWriter()
    desk, sink = desk_with(model, hf, writer=writer, duo_calls=2)
    first = goal_card(id="card-1", event_id="1")
    second = goal_card(id="card-2", event_id="2", clock_s=4100.0, minute_label="68'", player={"id": "home-9", "name": "Elio Ingleby"})
    desk.write(first, TEAMS)
    assert desk.runner.wait(10)
    desk.write(second, TEAMS)
    desk.close()
    assert desk.calls == {"m": len(writer.calls)}  # the Writer's cap counts the Writer's calls only (a retry when its duo repeats the first card's)
    assert desk.runner.budget.used("m") == 2
    assert sink.cards["card-1"]["duo_writer"] == "agents:scripted"
    (entry,) = [e for e in sink.audit if e["card_id"] == "card-2" and e["action"] == "duo_fallback"]
    assert entry["reason"] == "budget" and sink.cards["card-2"]["duo_trace"]["applied"] is False
    assert writer.calls[0]["duo"] is False and writer.calls[1]["duo"] is True  # the second card was not handed to the agents: the Writer wrote its duo
    assert len(model.clients["wren"].requests) == 1


def test_a_job_is_submitted_only_with_the_runs_of_its_turns(hf):
    go_on = threading.Event()

    def wren(messages):
        go_on.wait(10)
        return react()

    desk, sink = desk_with(ScriptedModel([wren], [card_detail()]), hf, duo_calls=5)
    first = goal_card(id="card-1", event_id="1")
    desk.write(first, TEAMS)  # two turns reserved: three runs free
    assert desk.runner.budget.left("m") == 3
    season = {"facts": {"score_now": "Varleigh 2-1 Yarnford", "varleigh_rank_at_kickoff": 18, "varleigh_rank_if_score_holds": 15, "varleigh_relegation_chance_if_score_holds_pct": 20},
              "meanings": {}}
    table = goal_card(id="card-2", event_id="2", clock_s=4100.0, minute_label="68'", score_before={"home": 1, "away": 1}, season_context=season)
    desk.write(table, TEAMS)  # four turns: the table
    go_on.set()
    desk.close()
    (entry,) = [e for e in sink.audit if e["card_id"] == "card-2" and e["action"] == "duo_fallback"]
    assert entry["reason"] == "budget" and sink.cards["card-2"]["duo_writer"] == "template"
    assert any(l["claims"] and l["claims"][0]["source_field"].startswith("season_") for l in sink.cards["card-2"]["duo"])  # the plan would have needed four turns
    assert sink.cards["card-1"]["duo_writer"] == "agents:scripted" and desk.runner.budget.used("m") == 2


def test_a_failed_hand_over_keeps_the_template_and_never_stops_the_pipeline(hf):
    class BrokenRegistry(FactRegistry):
        def put_state(self, *args) -> None:
            raise RuntimeError("broken")

    registry = BrokenRegistry()
    sink = MemorySink()
    desk = AgentDesk(sink, TemplateWriter(), runner=DuoRunner(ScriptedModel([], []), registry, hf), registry=registry, auto_approve=True)
    card = goal_card()
    assert desk.write(card, TEAMS) is card
    stored = sink.cards[card["id"]]
    assert stored["duo_writer"] == "template" and stored["status"] == "approved" and stored["duo_trace"]["reason"] == "error:RuntimeError"
    assert [e["reason"] for e in sink.audit if e["action"] == "duo_fallback"] == ["error:RuntimeError"]
    assert desk.runner.budget.left("m") == 36 and desk.runner.wait(1)  # no job, nothing reserved
    desk.close()


def test_only_eligible_cards_lose_the_writer_duo(hf):
    writer = FakeWriter()
    desk, _ = desk_with(ScriptedModel([react()], [card_detail()]), hf, writer=writer)
    desk.write(goal_card(id="card-big", event_id="big"), TEAMS)
    desk.write(goal_card(id="card-small", event_id="small", value_pts=5, clock_s=4200.0, minute_label="70'"), TEAMS)
    insight = {"id": "insight-m-sub4", "type": "insight", "match_id": "m", "key": "sub4", "kind": "record", "minute_label": "83'", "clock_s": 5000.0,
               "headline_facts": {"player": "Marijn Scarpetta", "goals_by_substitute": 4}, "field_meanings": {"goals_by_substitute": "goals by the substitute"}}
    desk.write_insight(insight, TEAMS)
    desk.close()
    by_card = {c["card"]: c["duo"] for c in writer.calls}
    assert by_card == {"card-big": False, "card-small": True, "insight-m-sub4": True}


# --- the demo match, dry -------------------------------------------------------------------------------------

def test_the_demo_match_with_the_dry_duo(history, hf):
    stream, _ = generate_match(7, "demo")
    registry = FactRegistry()
    sink = MemorySink()
    desk = AgentDesk(sink, TemplateWriter(), runner=DuoRunner(DuoModel(dry=True), registry, hf), registry=registry, auto_approve=True)
    pipeline = Pipeline(sink=sink, desk=desk, history=history, _history_view=hf.view)
    for m in stream:
        pipeline.handle(m)
    pipeline.flush()
    desk.close()
    cards = sorted(sink.cards.values(), key=lambda c: c["clock_s"])
    eligible = [c for c in cards if c["value_pts"] >= 10 and not low_stake(c)]
    assert {c["minute_label"] for c in eligible} >= {"14'", "31'", "52'", "57'", "66'", "72'"}
    for c in eligible:
        if c["status"] == "retracted":
            continue
        assert c["duo_writer"] == "agents:dry" and c["duo_trace"]["applied"], (c["minute_label"], c.get("duo_trace"))
        facts = card_facts(c, TEAMS)[0] | {k: v["value"] for k, v in c["duo_trace"]["tool_facts"].items()}
        assert verify_duo(c["duo"], LANGS, facts)["verified"], c["minute_label"]
    assert any(t["name"] == "player_history" for c in cards for turn_ in (c.get("duo_trace") or {}).get("turns", []) for t in turn_["tools"])
    for c in cards:
        if c["value_pts"] < 10 or low_stake(c):
            assert c["duo_writer"] == "template" and "duo_trace" not in c, c["minute_label"]
        if c["status"] == "retracted":
            assert c["duo_ruling"], c["minute_label"]
    assert desk.runner.budget.used("demo-0007") <= 36
    for c in eligible:
        outcomes = [e for e in sink.audit if e["card_id"] == c["id"] and e["action"] in ("duo_written", "duo_fallback")]
        assert len([e for e in outcomes if e["version"] == c["version"]]) == 1, (c["minute_label"], outcomes)
        assert len(outcomes) <= c["version"]


# --- the pipeline's poll --------------------------------------------------------------------------------------

class PollingDesk:
    """A desk that only counts the pipeline's polls."""

    def __init__(self) -> None:
        self.polls = 0

    def poll(self) -> int:
        self.polls += 1
        return 0

    def write(self, card, teams):
        return card

    def on_action(self, card, action, teams):
        return card


def test_a_duo_that_cannot_be_applied_never_stops_the_pipeline(hf, monkeypatch):
    desk, sink = desk_with(ScriptedModel([react()] * 2, [card_detail()] * 2), hf, auto_approve=True)
    desk.write(goal_card(id="card-1", event_id="1"), TEAMS)
    desk.write(goal_card(id="card-2", event_id="2", clock_s=4100.0, minute_label="68'"), TEAMS)
    assert desk.runner.wait(10)
    real = desk._apply

    def apply(result):
        if result.card_id == "card-1":
            raise KeyError("a broken card")
        return real(result)

    monkeypatch.setattr(desk, "_apply", apply)
    assert desk.poll() == 1
    assert sink.cards["card-1"]["duo_writer"] == "template" and sink.cards["card-2"]["duo_writer"] == "agents:scripted"
    desk.close()


def test_the_worker_lists_the_new_options():
    out = subprocess.run([sys.executable, "-m", "worker", "--help"], capture_output=True, text=True, check=True, env={**os.environ, "PYTHONPATH": str(SRC)}).stdout
    for option in ("--facts", "--duo", "--duo-dry", "--duo-calls", "--duo-min-value", "--duo-deadline"):
        assert option in out, option


def test_the_worker_runs_the_dry_duo_end_to_end(tmp_path, history):
    replay = SRC.parent / "data" / "replay" / "demo-0007.jsonl.gz"
    args = [sys.executable, "-m", "worker", str(replay), "--writer", "template", "--duo", "agents", "--duo-dry", "--history", str(history.save(tmp_path / "history")),
            "--desk-dir", str(tmp_path / "desk")]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FOUNDRY_", "AZURE_"))} | {"PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"}
    run = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
    assert run.returncode == 0, run.stderr[-2000:]
    lines = run.stdout.splitlines()
    final = lines[-1]
    assert final.startswith("duo agents (dry): ") and final.endswith(" of 36 runs") and ", {} template, " in final, final
    written = int(final.split(": ")[1].split()[0])
    assert written >= 6 and any(l.strip().startswith("duo agents:dry: wren, joss (player_history)") for l in lines)
    cards = json.loads((tmp_path / "desk" / "demo-0007" / "cards.json").read_text(encoding="utf-8"))
    assert 1 <= sum(1 for c in cards if c["duo_writer"] == "agents:dry") <= written  # a card rewritten after its duo counts twice in the run's line
    audit = [json.loads(l) for l in (tmp_path / "desk" / "demo-0007" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(1 for e in audit if e["action"] == "duo_written") == written and any(t.startswith("player_history(") for e in audit for t in e.get("tools", []))


def test_the_pipeline_polls_a_background_desk():
    stream, _ = generate_match(7, "demo")
    desk = PollingDesk()
    pipeline = Pipeline(desk=desk)
    for m in stream[:50]:
        pipeline.handle(m)
    assert desk.polls == 50
    pipeline.flush()
    assert desk.polls == 51
    assert not hasattr(Desk(MemorySink()), "poll")


def test_texts_only_asks_the_writer_for_the_texts_and_draft_adds_the_template_duo():
    seen = []

    class Fake:
        name = "fake"

        async def write_async(self, card, facts, languages, note=None, avoid=None, duo=False):
            seen.append(duo)
            return TemplateWriter().write(card, facts[0], languages)

    d = asyncio.run(draft(goal_card(), TEAMS, TextsOnly(Fake()), LANGS, duo=True))
    assert seen == [False] and (d.writer, d.duo_writer) == ("fake", "template") and d.duo and d.report["verified"]


def test_texts_only_keeps_the_writer_trace_and_token_budget():
    class Traced:
        name, max_tokens, config = "traced", 3000, {"deployment": "traced"}

        def __init__(self) -> None:
            self.last_call, self.seen = None, []

        async def write_async(self, card, facts, languages, note=None, avoid=None, duo=False, preferences=None, max_tokens=None):
            self.seen.append(max_tokens)
            self.last_call = {"finish_reason": "stop", "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}, "latency_s": 0.5}
            return TemplateWriter().write(card, facts[0], languages)

    inner = Traced()
    d = asyncio.run(draft(goal_card(), TEAMS, TextsOnly(inner), LANGS, duo=True))
    assert d.writer_config == {"deployment": "traced"} and d.attempts_log[0]["latency_s"] == 0.5 and d.attempts_log[0]["finish_reason"] == "stop"
    assert asyncio.run(TextsOnly(inner).write_async(goal_card(), card_facts(goal_card(), TEAMS), LANGS, max_tokens=4500)) and inner.seen[-1] == 4500
