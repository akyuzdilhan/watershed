import math
from pathlib import Path

import pytest

from generator import generate_match
from web import matchfeed
from worker import tactics as T

REPLAY = Path(__file__).resolve().parents[1] / "data" / "replay" / "demo-0007.jsonl.gz"


@pytest.fixture(scope="module")
def demo():
    return matchfeed.build(matchfeed.load(REPLAY))


def by_id(feed, event_id):
    return next(a for a in feed["actions"] if a["event_id"] == event_id)


def player(pid, x, y, teammate, actor=False, keeper=False):
    return {"player_id": pid, "teammate": teammate, "actor": actor, "keeper": keeper, "location": [x, y]}


def action(event_id, team, pid, sx, sy, ex, ey, frame, action_type="pass", result="success", seq=1, period=1, clock_s=60.0):
    return {"event_id": event_id, "seq": seq, "team": team, "player": {"id": pid, "shirt": int(pid.split("-")[1]), "name": pid}, "action_type": action_type, "result": result,
            "start_x": sx, "start_y": sy, "end_x": ex, "end_y": ey, "freeze_frame": frame, "period": period, "clock_s": clock_s, "minute_label": "2'", "possession": 1}


# --- the feed with its corrections --------------------------------------------------------------------

def test_corrections_are_applied_to_the_actions(demo):
    voided = {a["minute_label"]: a["void_cause"] for a in demo["actions"] if a.get("voided")}
    assert voided == {"63'": "var_offside", "69'": "var_handball"}
    assert by_id(demo, "demo-0007-v2-a00562")["player"]["id"] == "away-7"  # 31' goal reassigned to #7
    assert by_id(demo, "demo-0007-v2-a01178")["offside"] == {"player_id": "home-20", "margin_m": 0.25}


def test_the_feed_can_be_cut_at_the_desk_position():
    messages = matchfeed.load(REPLAY)
    cut = matchfeed.build(messages, upto_seq=600)
    assert cut["last_seq"] <= 600 and max(a["seq"] for a in cut["actions"]) <= 600
    assert not any(a.get("voided") for a in cut["actions"])


# --- detectors, checked by hand ----------------------------------------------------------------------

def test_defenders_bypassed_by_hand_on_a_synthetic_frame():
    frame = [player("home-8", 40, 30, True, actor=True), player("home-9", 70, 30, True),
             player("away-2", 35, 20, False), player("away-3", 45, 40, False), player("away-4", 55, 34, False), player("away-5", 69.9, 30, False),
             player("away-6", 71, 30, False), player("away-1", 100, 34, False, keeper=True)]
    a = action("e1", "home", "home-8", 40, 30, 70, 30, frame)
    assert T.defenders_bypassed(a) == ["away-3", "away-4", "away-5"]  # 45, 55 and 69.9 lie between 40 and 70; 35 behind, 71 beyond, the keeper never counts
    backwards = action("e2", "home", "home-8", 40, 30, 30, 30, frame)
    assert T.defenders_bypassed(backwards) == []


def test_defenders_bypassed_by_hand_on_the_82nd_minute_pass(demo):
    a = by_id(demo, "demo-0007-v2-a01454")
    assert (a["start_x"], a["end_x"]) == (48.9, 74.8)
    by_hand = {"away-10": 51.7, "away-17": 49.7, "away-19": 49.6, "away-3": 73.5, "away-4": 60.4, "away-6": 74.5, "away-8": 64.0}  # outfield, 48.9 < x < 74.8
    outside = {"away-2": 77.6, "away-20": 41.9}
    frame = {p["player_id"]: p["location"][0] for p in a["freeze_frame"] if not p["teammate"]}
    assert all(frame[pid] == x for pid, x in {**by_hand, **outside}.items())
    assert T.defenders_bypassed(a) == sorted(by_hand)
    row = next(r for r in T.line_breaks(demo["actions"]) if r["event_id"] == a["event_id"])
    assert row["bypassed"] == 7 and row["passer_id"] == "home-10"


def test_keepers_long_balls_are_not_line_breaks(demo):
    keepers = {a["event_id"] for a in demo["actions"] if any(p.get("actor") and p.get("keeper") for p in a.get("freeze_frame") or [])}
    assert keepers and not keepers & {r["event_id"] for r in T.line_breaks(demo["actions"])}


def test_the_63rd_minute_run_is_a_quarter_metre_beyond_the_line(demo):
    r = next(r for r in T.runs_behind(demo["actions"]) if r["event_id"] == "demo-0007-v2-a01178")
    assert r["runner_id"] == "home-20" and r["defender_id"] == "away-6"
    assert r["line_x"] == 91.6 and r["runner_x"] == 91.8
    assert r["frame_margin_m"] == 0.2 and r["margin_m"] == 0.25 and r["official"]
    a = by_id(demo, "demo-0007-v2-a01178")
    keeper_and_outfield = sorted((p["location"][0] for p in a["freeze_frame"] if not p["teammate"]), reverse=True)
    assert keeper_and_outfield[1] == r["line_x"]  # second-last defender, keeper included


def test_space_at_reception_by_hand():
    pass_frame = [player("home-8", 40, 30, True, actor=True), player("away-4", 60, 30, False)]
    recv_frame = [player("home-9", 50, 30, True, actor=True), player("away-4", 53, 34, False), player("away-5", 60, 30, False), player("away-1", 100, 34, False, keeper=True)]
    p = action("p", "home", "home-8", 40, 30, 50, 30, pass_frame, seq=1)
    r = action("r", "home", "home-9", 50.5, 30, 60, 30, recv_frame, action_type="dribble", seq=2)
    rows = T.space_at_reception([p, r])
    assert len(rows) == 1 and rows[0]["space_m"] == 5.0 and rows[0]["nearest_id"] == "away-4"


def test_pressure_and_line_height_by_hand():
    frame = [player("home-9", 60, 30, True, actor=True), player("home-4", 30, 20, True), player("home-1", 10, 34, True, keeper=True),
             player("away-4", 63, 34, False), player("away-5", 60, 35.1, False), player("away-6", 80, 30, False), player("away-1", 100, 34, False, keeper=True)]
    a = action("a", "home", "home-9", 60, 30, 70, 30, frame, action_type="dribble")
    row = T.pressure([a])[0]
    assert row["pressing_team"] == "away" and row["pressers"] == 1 and row["presser_ids"] == ["away-4"]  # 5.0 m counts, 5.1 m does not
    h = T.line_heights([a])[0]
    assert h["home"] == 30 and h["away"] == 25  # home's deepest outfield player at 30 m; away's at 80 m in home's frame, 25 m from its own goal


def test_location_value_matches_the_generator_xg():
    from generator.pitch import xg

    for x, y in ((94, 34), (88.5, 20), (60, 50)):
        assert math.isclose(T.location_value(x, y), xg(x, y), rel_tol=1e-9)


# --- insight candidates ------------------------------------------------------------------------------

def check_contract(doc, match_id):
    assert doc["id"] == f"insight-{match_id}-{doc['key']}" and doc["type"] == "insight" and doc["kind"] == "tactical"
    assert set(doc["headline_facts"]) <= set(doc["field_meanings"])
    assert 0 <= doc["value"] <= 100 and doc["status"] == "proposed"
    assert {"p", "surprisal_bits", "basis"} <= set(doc["rarity"])
    assert doc["figure"]["kind"] in ("line_break", "run", "space", "line_height", "pressing")


def test_demo_candidates_name_the_63rd_minute_run_and_the_line_break_before_a_goal(demo):
    docs = T.insight_candidates(demo["actions"], demo["info"], "demo-0007")
    for d in docs:
        check_contract(d, "demo-0007")
    assert [d["value"] for d in docs] == sorted((d["value"] for d in docs), reverse=True)
    run = next(d for d in docs if d["key"] == "run-demo-0007-v2-a01178")
    f = run["headline_facts"]
    assert f["margin_m"] == 0.25 and f["runner_beyond_line"] is True and f["goal_stood"] is False and f["runner_name"] == "#20 Dorian Lightfoot"
    assert "official VAR figure" in run["field_meanings"]["margin_m"]
    lb = next(d for d in docs if d["figure"]["kind"] == "line_break")
    pass_action = by_id(demo, lb["figure"]["event_id"])
    assert lb["headline_facts"]["defenders_bypassed"] == len(T.defenders_bypassed(pass_action))
    assert lb["headline_facts"]["passes_bypassing_as_many"] == sum(1 for r in T.line_breaks(demo["actions"]) if r["bypassed"] >= lb["headline_facts"]["defenders_bypassed"])


@pytest.mark.parametrize("seed", [1, 3, 14, 21])
def test_candidates_on_unscripted_seeds_follow_the_contract(seed):
    stream, _ = generate_match(seed)
    feed = matchfeed.build(stream)
    docs = T.insight_candidates(feed["actions"], feed["info"])
    for d in docs:
        check_contract(d, feed["actions"][0]["match_id"])
        if d["figure"]["kind"] == "run":
            r = next(r for r in T.runs_behind(feed["actions"]) if r["event_id"] == d["figure"]["event_id"])
            assert d["headline_facts"]["margin_m"] == abs(r["margin_m"])
