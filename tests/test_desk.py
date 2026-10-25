import json

import pytest

from generator import generate_match
from worker.desk import Desk
from worker.pipeline import MemorySink, Pipeline
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
        for t in card["texts"]:
            assert "100%" not in t["why"] and "100 %" not in t["why"]


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
