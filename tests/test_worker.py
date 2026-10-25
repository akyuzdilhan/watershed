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
    assert all(1 <= v <= 99 for c in spotter.cards.values() for k, v in c["facts"].items() if k != "xg_pct"), "a card is always before the final whistle"


def test_unreadable_messages_are_refused():
    with pytest.raises(SchemaError):
        Match("m").receive({"schema_version": "0.1.0", "message_id": "x", "seq": 1})


# --- what the card keeps for the desk and the console ----------------------------------------------

def test_card_percentages_stay_between_1_and_99(demo):
    stream, _ = demo
    _, results = run(stream)
    cards = [r for r in results if "action" not in r]
    for c in cards:
        for k, v in c["facts"].items():
            if k != "xg_pct":
                assert 1 <= v <= 99, (c["minute_label"], k, v)
    assert max(c["facts"]["team_wp_before_pct"] for c in cards) == 99  # the last goal: a side at 100% before rounding is shown at 99%


def test_a_red_card_says_which_card(demo):
    stream, _ = demo
    _, results = run(stream)
    shown = {m["event_id"]: m["card"] for m in stream if m["type"] == "action" and m.get("card") in ("red", "second_yellow")}
    cards = [r for r in results if "action" not in r]
    reds = [c for c in cards if c["moment"] == "red_card"]
    assert reds and all(c["card_kind"] == shown[c["event_id"]] for c in reds)
    assert not any("card_kind" in c for c in cards if c["moment"] != "red_card")


def test_a_rewrite_names_the_previous_player(demo):
    stream, truth = demo
    pipeline, _ = run(stream)
    goal = next(g["event_id"] for g in truth["goals"] if g["expected_card_action"] == "rewrite")
    first = next(m for m in stream if m["type"] == "action" and m["event_id"] == goal)
    fix = next(m for m in stream if m["type"] == "correction" and m["target_event_id"] == goal and "player" in m["changes"])
    cards = pipeline.spotters["demo-0007"].cards
    card = cards[goal]
    assert first["player"]["name"] != fix["changes"]["player"]["name"]
    assert card["version"] == 2 and card["player"] == fix["changes"]["player"] and card["previous_player"] == first["player"]["name"]
    assert not any("previous_player" in c for e, c in cards.items() if e != goal)


def test_previous_player_follows_each_version(demo):
    """Credited back to the first scorer: the previous player is the one of version 2; credited again to the same player: no previous player."""
    stream, truth = demo
    goal = next(g["event_id"] for g in truth["goals"] if g["expected_card_action"] == "rewrite")
    first = next(m for m in stream if m["type"] == "action" and m["event_id"] == goal)
    fix = next(m for m in stream if m["type"] == "correction" and m["target_event_id"] == goal and "player" in m["changes"])
    seq, last = max(m["seq"] for m in stream), max(m["emitted_at_s"] for m in stream)
    back = {**fix, "message_id": "extra-back", "seq": seq + 1, "changes": {"player": first["player"]}, "new_version": 3, "emitted_at_s": last + 10}
    again = {**back, "message_id": "extra-again", "seq": seq + 2, "new_version": 4, "emitted_at_s": last + 20}
    pipeline = Pipeline()
    for m in list(stream) + [back]:
        pipeline.handle(m)
    pipeline.flush()
    card = pipeline.spotters["demo-0007"].cards[goal]
    assert card["version"] == 3 and card["player"] == first["player"] and card["previous_player"] == fix["changes"]["player"]["name"]
    pipeline.handle(again)
    pipeline.flush()
    assert card["version"] == 4 and "previous_player" not in card


def test_a_void_records_the_var_decision(demo):
    stream, _ = demo
    pipeline, _ = run(stream)
    cards = pipeline.spotters["demo-0007"].cards
    voids = {m["target_event_id"]: m for m in stream if m["type"] == "correction" and m["op"] == "void"}
    margins = {m["related_message_id"]: m["changes"]["offside"]["margin_m"] for m in stream if m["type"] == "correction" and isinstance(m["changes"].get("offside"), dict)}
    calls = {}
    for event_id, void in voids.items():
        decision = cards[event_id]["var_decision"]
        margin = margins.get(void["related_message_id"])
        assert (decision["reason"], decision["margin_m"]) == (void["cause"], margin)
        calls[void["cause"]] = decision["call"]
    assert calls == {"var_offside": "tight", "var_handball": None}  # 0.25 m: within what a sprinting player covers between two frames
    assert not any("var_decision" in c for e, c in cards.items() if e not in voids)
