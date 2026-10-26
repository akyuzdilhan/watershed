import json

import pytest

from generator import generate_match
from worker.desk import Desk
from worker.pipeline import MemorySink, Pipeline
from worker.spotter import score_against_truth
from worker.writer import TemplateWriter, card_facts, verify


@pytest.fixture(scope="module")
def desk_run():
    stream, truth = generate_match(7, "demo")
    sink = MemorySink()
    desk = Desk(sink, TemplateWriter(), auto_approve=True)
    pipeline = Pipeline(sink=sink, desk=desk)
    for m in stream:
        pipeline.handle(m)
    pipeline.flush()
    return sink, desk, truth


def test_every_card_is_written_in_three_languages_and_verified(desk_run):
    sink, _, _ = desk_run
    assert len(sink.cards) == 11
    for card in sink.cards.values():
        assert sorted(t["lang"] for t in card["texts"]) == ["en", "es", "fr"]
        assert card["verification"]["verified"] and card["machine_translated"] == ["es"]


def test_retracted_cards_come_off_air(desk_run):
    sink, _, truth = desk_run
    disallowed = [g["event_id"] for g in truth["goals"] if not g["counts"]]
    for event_id in disallowed:
        card = sink.cards[f"card-{event_id}"]
        assert card["status"] == "retracted" and card["retracted_on_air"]
    with pytest.raises(ValueError):
        Desk(MemorySink()).approve(dict(sink.cards[f"card-{disallowed[0]}"]), "producer")


def test_a_reassigned_goal_is_rewritten_with_the_new_scorer(desk_run):
    sink, _, truth = desk_run
    goal = next(g["event_id"] for g in truth["goals"] if g["expected_card_action"] == "rewrite")
    card = sink.cards[f"card-{goal}"]
    assert card["version"] == 2 and card["player"]["id"] == "away-7"
    assert all(card["player"]["name"] in t["headline"] for t in card["texts"])
    assert [h["action"] for h in card["history"]].count("written") == 2


def test_no_probability_of_0_or_100_before_the_final_whistle(desk_run):
    sink, _, _ = desk_run
    for card in sink.cards.values():
        assert all(1 <= v <= 99 for k, v in card["facts"].items() if k != "xg_pct"), card["minute_label"]
        for t in card["texts"]:
            assert "100%" not in t["why"] and "100 %" not in t["why"]
        for line in card.get("duo", []):
            assert "100%" not in line["line"] and "100 %" not in line["line"]


def test_each_card_action_is_audited_once(desk_run):
    """With a desk, each card action from the feed has one audit entry, the desk's, with the action's own event, message and minute; the score against the ground truth reads it."""
    sink, _, truth = desk_run
    stream, _ = generate_match(7, "demo")
    bare = Pipeline()
    actions = [r for m in stream for r in bare.handle(m) if "action" in r] + [r for r in bare.flush() if "action" in r]
    named = {"retract": "retracted", "rewrite": "rewrite_requested", "hold": "hold", "release": "release"}
    feed = [e for e in sink.audit if e["by"] == "feed"]
    assert sorted((e["action"], e["event_id"], e["message_id"], e["minute_label"]) for e in feed) == sorted(
        (named[a["action"]], a["event_id"], a["message_id"], a["minute_label"]) for a in actions)
    assert all(e.get("by") for e in sink.audit)  # no bare line from the Spotter next to the desk's
    for card in sink.cards.values():
        assert [e["action"] for e in sink.audit if e["card_id"] == card["id"]] == [h["action"] for h in card["history"]], card["minute_label"]
    report = score_against_truth({c["event_id"]: c for c in sink.cards.values()}, sink.audit, truth)
    assert report["card_actions_correct"] and report["card_actions_done"] == report["card_actions_expected"]


def test_producer_decisions_are_audited():
    stream, _ = generate_match(7, "demo")
    sink = MemorySink()
    desk = Desk(sink, TemplateWriter())
    pipeline = Pipeline(sink=sink, desk=desk)
    for m in stream[:600]:
        pipeline.handle(m)
    card = next(c for c in sink.cards.values() if c["status"] == "pending")
    edited = [dict(t, headline=t["headline"] + "!") for t in card["texts"]]
    desk.approve(card, "producer-1", texts=edited)
    assert card["status"] == "approved" and card["texts"][0]["headline"].endswith("!")
    assert [e["action"] for e in sink.audit if e.get("card_id") == card["id"]][-2:] == ["edited", "approved"]


def test_verifier_rejects_invented_numbers_and_misused_percentages():
    card = {"id": "c", "team": "home", "team_name": "Varleigh", "moment": "save", "minute_label": "72'", "player": {"name": "Keeper"},
            "score_before": {"home": 3, "away": 2}, "facts": {"xg_pct": 20, "team_wp_before_pct": 81, "team_wp_if_goal_pct": 24}}
    facts, _ = card_facts(card, {"home": "Varleigh", "away": "Yarnford"})
    good = [{"headline": "Keeper saves", "why": "Varleigh stay at 81% instead of 24%.", "lang": "en",
             "claims": [{"value": 81, "source_field": "varleigh_wp_after_save_pct"}, {"value": 24, "source_field": "varleigh_wp_if_goal_pct"}]}]
    assert verify(json.dumps(good), ["en"], facts)[1]["verified"]
    invented = [dict(good[0], why="Varleigh stay at 81% instead of 24%, his 12th save.")]
    assert not verify(json.dumps(invented), ["en"], facts)[1]["verified"]
    wrong_unit = [dict(good[0], why="Varleigh stay at 81% instead of 57%.", claims=good[0]["claims"] + [{"value": 57, "source_field": "save_value_pts"}])]
    assert not verify(json.dumps(wrong_unit), ["en"], facts)[1]["verified"]
    assert not verify("not json", ["en"], facts)[1]["verified"]
