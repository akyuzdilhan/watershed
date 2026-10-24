import json
import sys

import pytest

from generator import generate_match
from worker import recap as recap_cli
from worker.desk import Desk
from worker.pipeline import JsonFileSink, MemorySink, Pipeline
from worker.recap import MAX_WORDS, analyse, build_recap, recap_facts, verify_recap
from worker.writer import TemplateWriter


def run_desk(seed: int, story: str = "random", sink=None, frames: bool = False):
    stream, truth = generate_match(seed, story, frames=frames)
    sink = sink or MemorySink()
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True))
    for m in stream:
        pipeline.handle(m)
    pipeline.flush()
    return list(sink.cards.values()), next(iter(sink.states.values())), truth


@pytest.fixture(scope="module")
def demo():
    cards, state, truth = run_desk(7, "demo")
    return cards, state, truth, build_recap(cards, state)


def test_demo_recap_is_verified_in_three_languages(demo):
    _, _, _, recap = demo
    assert recap["final"] and recap["writer"] == "template" and recap["verification"]["verified"]
    assert sorted(t["lang"] for t in recap["texts"]) == ["en", "es", "fr"] and recap["machine_translated"] == ["es"]
    for t in recap["texts"]:
        for section, limit in MAX_WORDS.items():
            assert 0 < len(t[section].split()) <= limit
    assert recap["facts"]["final_score"] == "Rovers 5-2 United" and recap["facts"]["half_time_score"] == "Rovers 0-2 United"


def test_turning_point_is_a_second_half_rovers_goal(demo):
    cards, _, truth, recap = demo
    tp = recap["analysis"]["turning_point"]
    rovers_second_half = {f"card-{g['event_id']}" for g in truth["goals"] if g["counts"] and g["team"] == "home" and g["period"] == 2}
    assert tp["card_id"] in rovers_second_half and tp["player"] == "Dorian Lightfoot" and tp["swing_pts"] > 30
    assert recap["analysis"]["team_summary"]["home"]["scorers"][0] == {"name": "Dorian Lightfoot", "goals": 4, "minutes": ["52'", "57'", "66'", "83'"]}


def test_no_retracted_card_is_cited(demo):
    cards, state, truth, recap = demo
    status = {c["id"]: c["status"] for c in cards}
    retracted = [c for c in cards if c["status"] == "retracted"]
    assert len(retracted) == 2
    assert all(status[i] == "approved" for i in recap["cited_cards"] + recap["analysis"]["cards_on_air"])
    voided = {c["event_id"] for c in retracted}
    assert all(p["minute_label"] not in ("63'", "69'") for p in recap["analysis"]["wp_curve"])
    text = json.dumps(recap["texts"], ensure_ascii=False) + json.dumps(recap["facts"], ensure_ascii=False)
    assert "63'" not in text and "69'" not in text and not voided & set(recap["analysis"]["cards_on_air"])


def test_only_approved_cards_count(demo):
    cards, state, _, _ = demo
    tp = analyse(cards, state)["turning_point"]["card_id"]
    others = [dict(c, status="rejected") if c["id"] == tp else c for c in cards]
    recap = build_recap(others, state)
    assert recap["analysis"]["turning_point"]["card_id"] != tp and tp not in recap["cited_cards"]
    assert "half_time_score" not in recap["facts"]  # the approved goals no longer add up to the final score
    assert recap["verification"]["verified"]


def test_fan_text_for_the_losing_side(demo):
    cards, state, _, _ = demo
    recap = build_recap(cards, state, fan_side="away")
    assert recap["fan_team"] == "United" and recap["verification"]["verified"]
    en = next(t for t in recap["texts"] if t["lang"] == "en")
    assert en["fan"].startswith("Hard to take for United")


def test_verifier_rejects_invented_numbers_names_and_retracted_moments(demo):
    cards, state, _, recap = demo
    facts = recap["facts"]
    good = [dict(t) for t in recap["texts"]]
    assert verify_recap(json.dumps(good), ["en", "fr", "es"], facts, cards)[1]["verified"]
    cases = {
        "invented number": "Rovers won with 61% possession.",
        "retracted goal": "Dorian Lightfoot scored at 63' too.",
        "unknown player": "Rovers won, Umberto Valtonen starred.",
        "spelled number": "Dorian Lightfoot scored four goals.",
        "too long": " ".join(["Rovers"] * 61),
    }
    for case, text in cases.items():
        bad = [dict(good[0], recap=text)] + good[1:]
        assert not verify_recap(json.dumps(bad), ["en", "fr", "es"], facts, cards)[1]["verified"], case
    assert not verify_recap(json.dumps(good[:2]), ["en", "fr", "es"], facts, cards)[1]["verified"]
    assert not verify_recap("not json", ["en"], facts, cards)[1]["verified"]


@pytest.mark.parametrize("seed", range(1, 13))
def test_unscripted_matches_get_a_verified_recap(seed):
    cards, state, _ = run_desk(seed)
    recap = build_recap(cards, state)
    assert recap["verification"]["verified"], recap["verification"]["report"]
    facts, _, roles = recap_facts(recap["analysis"])
    assert roles["fan_result"] in ("won", "draw")


class FailingWriter:
    name = "flaky-model"

    def __init__(self) -> None:
        self.calls = 0

    async def write_async(self, match_id, facts, languages, note=None):
        self.calls += 1
        return json.dumps([{"lang": lang, "recap": "Rovers won 9-0.", "fan": "Yes.", "analyst": "Yes.", "claims": []} for lang in languages])


def test_a_failing_model_gets_one_retry_then_the_template(demo):
    cards, state, _, _ = demo
    writer = FailingWriter()
    recap = build_recap(cards, state, writer)
    assert writer.calls == 2 and recap["writer"] == "template" and recap["verification"] == {**recap["verification"], "verified": True, "attempts": 2}


def test_cli_writes_recap_next_to_the_cards(tmp_path, monkeypatch, capsys):
    run_desk(7, "demo", sink=JsonFileSink(tmp_path))
    monkeypatch.setattr(sys, "argv", ["recap", str(tmp_path / "demo-0007")])
    recap_cli.main()
    written = json.loads((tmp_path / "demo-0007" / "recap.json").read_text(encoding="utf-8"))
    assert written["verification"]["verified"] and written["id"] == "recap-demo-0007"
    assert json.loads(capsys.readouterr().out)["id"] == "recap-demo-0007"
