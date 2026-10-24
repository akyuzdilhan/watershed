import random

import pytest

from generator import generate_match
from worker.match import Match, SchemaError
from worker.pipeline import Pipeline
from worker.spotter import score_against_truth


@pytest.fixture(scope="module")
def demo():
    return generate_match(7, "demo")


def run(stream):
    pipeline = Pipeline()
    results = [r for m in stream for r in pipeline.handle(m)] + pipeline.flush()
    return pipeline, results


def test_demo_cards_and_actions_match_the_ground_truth(demo):
    stream, truth = demo
    pipeline, _ = run(stream)
    spotter = next(iter(pipeline.spotters.values()))
    report = score_against_truth(spotter.cards, pipeline.sink.audit, truth)
    assert (report["recall"], report["precision"], report["card_actions_correct"]) == (1.0, 1.0, True), report
    assert next(iter(pipeline.matches.values())).score() == truth["final_score"]


def test_the_central_card_is_held_then_retracted(demo):
    stream, truth = demo
    pipeline, results = run(stream)
    goal = next(m["event_id"] for m in truth["moments"] if m["key"] == "goal-63")
    actions = [r["action"] for r in results if r.get("event_id") == goal and "action" in r]
    assert actions == ["hold", "retract"]
    assert pipeline.sink.cards[f"card-{goal}"]["status"] == "retracted"


def test_the_save_card_credits_the_keeper(demo):
    stream, _ = demo
    pipeline, results = run(stream)
    (save,) = [r for r in results if r.get("moment") == "save"]
    assert (save["minute_label"], save["team"], save["facts"]["team_wp_before_pct"], save["facts"]["team_wp_if_goal_pct"]) == ("72'", "home", 81, 24)


def test_duplicates_and_disorder_do_not_change_the_result(demo):
    stream, _ = demo
    _, expected = run(stream)
    shuffled = list(stream)
    rng = random.Random(1)
    for i in range(0, len(shuffled) - 4, 4):  # swap neighbours, as a network would
        if rng.random() < 0.3:
            shuffled[i], shuffled[i + 2] = shuffled[i + 2], shuffled[i]
    shuffled += rng.sample(stream, 50)  # and send some messages again at the end
    pipeline, results = run(shuffled)
    key = lambda r: (r.get("action", "card"), r["event_id"])
    assert sorted(map(key, results)) == sorted(map(key, expected))
    assert next(iter(pipeline.matches.values())).duplicates >= 50


@pytest.mark.parametrize("seed", range(1, 21))
def test_unscripted_matches(seed):
    stream, truth = generate_match(seed, frames=False)
    pipeline, _ = run(stream)
    spotter = next(iter(pipeline.spotters.values()))
    report = score_against_truth(spotter.cards, pipeline.sink.audit, truth)
    assert report["recall"] == 1.0 and report["card_actions_correct"], report


def test_unreadable_messages_are_refused():
    with pytest.raises(SchemaError):
        Match("m").receive({"schema_version": "0.1.0", "message_id": "x", "seq": 1})
