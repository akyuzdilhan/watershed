import gzip
import json
import math
from pathlib import Path

import pytest

from generator import generate_match
from generator.calibration import TARGETS, summarise
from generator.cdf import write_cdf
from generator.clock import minute_label
from generator.publish import replay, schedule
from winprob.model import MatchState
from winprob.skellam import SkellamWinProb

SEEDS = range(1, 201)  # the unscripted matches behind the invariants and the calibration table
ENVELOPE = ("schema_version", "generator_version", "match_id", "message_id", "seq", "type", "wall_s", "emitted_at_s")
DEMO_FILE = Path(__file__).parent.parent / "data" / "replay" / "demo-0007.jsonl.gz"


@pytest.fixture(scope="module")
def demo():
    return generate_match(7, "demo")


@pytest.fixture(scope="module")
def matches():
    return [generate_match(seed, frames=False) for seed in SEEDS]


def in_order(stream):
    """Messages without duplicates, in provider order."""
    first = {}
    for m in stream:
        first.setdefault(m["message_id"], m)
    return sorted(first.values(), key=lambda m: m["seq"])


def actions(stream):
    return [m for m in in_order(stream) if m["type"] == "action"]


def test_same_seed_same_feed():
    a, b = generate_match(7, "demo"), generate_match(7, "demo")
    assert json.dumps(a) == json.dumps(b)
    assert generate_match(8, "demo")[0] != a[0]


def test_committed_demo_file_matches_the_generator(demo):
    if not DEMO_FILE.exists():
        pytest.skip("run python -m generator --story demo first")
    lines = gzip.decompress(DEMO_FILE.read_bytes()).decode("utf-8").splitlines()
    assert lines == [json.dumps(m, ensure_ascii=False) for m in demo[0]]


def test_demo_story(demo):
    stream, truth = demo
    moments = {m["key"]: (m["minute_label"], m["expected_card_action"], m["score_after"]) for m in truth["moments"]}
    assert moments == {
        "goal-14": ("14'", "none", "0-1"),
        "goal-31": ("31'", "rewrite", "0-2"),
        "yellow-41": ("41'", "none", "0-2"),
        "sub-46": ("46'", "none", "0-2"),
        "goal-52": ("52'", "none", "1-2"),
        "goal-57": ("57'", "none", "2-2"),
        "goal-63": ("63'", "retract", "2-2"),
        "goal-66": ("66'", "none", "3-2"),
        "goal-69": ("69'", "retract", "3-2"),
        "save-72": ("72'", "none", "3-2"),
        "red-78": ("78'", "none", "3-2"),
        "goal-83": ("83'", "none", "4-2"),
        "goal-88": ("88'", "none", "5-2"),
    }
    assert truth["final_score"] == {"home": 5, "away": 2}
    msgs = in_order(stream)
    by_id = {m["message_id"]: m for m in msgs}
    ids = {m["key"]: m["event_id"] for m in truth["moments"]}
    goals = [g for g in truth["goals"] if g["counts"]]
    assert sum(g["player_id"] == "home-20" for g in goals) == 4, "the substitute scores four goals that stand"
    sub = by_id[ids["sub-46"]]
    assert (sub["type"], sub["player_in"]["id"], sub["player_out"]["id"]) == ("substitution", "home-20", "home-9")

    reassign = next(m for m in msgs if m["type"] == "correction" and m["op"] == "reassign")
    assert (reassign["target_event_id"], reassign["minute_label"], reassign["changes"]["player"]["id"]) == (ids["goal-31"], "33'", "away-7")

    checks = [m for m in msgs if m["type"] == "var_check" and m["target_event_id"] == ids["goal-63"]]
    assert [c["status"] for c in checks] == ["started", "completed"] and checks[1]["decision"] == "overturned"
    after = [m for m in msgs if m["seq"] > checks[1]["seq"]]
    update, void = after[0], after[1]
    assert (update["op"], update["changes"]["result"], update["cause"]) == ("update", "offside", "var_offside")
    assert (void["op"], void["target_event_id"], void["score"]) == ("void", ids["goal-63"], {"home": 2, "away": 2})
    restart = next(m for m in after if m["type"] == "action")
    assert (restart["team"], restart["restart"]) == ("away", "free_kick")

    checks = [m for m in msgs if m["type"] == "var_check" and m["target_event_id"] == ids["goal-88"]]
    assert checks[-1]["decision"] == "stands"
    restart = next(m for m in msgs if m["type"] == "action" and m["seq"] > checks[-1]["seq"])
    assert (restart["team"], restart["restart"]) == ("away", "kick_off")

    handball = next(m for m in msgs if m["type"] == "correction" and m["target_event_id"] == ids["goal-69"])
    assert (handball["op"], handball["cause"], handball["score"]) == ("void", "var_handball", {"home": 3, "away": 2})
    restart = next(m for m in msgs if m["type"] == "action" and m["seq"] > handball["seq"])
    assert (restart["team"], restart["restart"]) == ("home", "free_kick")

    first, second = by_id[ids["yellow-41"]], by_id[ids["red-78"]]
    assert first["player"]["id"] == second["player"]["id"] == "away-5"
    assert (first["card"], second["card"], second["result"]) == ("yellow", "second_yellow", "red_card")
    save = by_id[ids["save-72"]]
    assert (save["team"], save["shot"]["outcome"], save["score"]) == ("away", "saved", {"home": 3, "away": 2})


def test_offside_is_visible_in_the_freeze_frame(demo):
    stream, truth = demo
    goal = next(g for g in truth["goals"] if g["planted"] == "goal-63")
    through_ball = next(m for m in actions(stream) if m["event_id"] in goal["affected_event_ids"] and m["event_id"] != goal["event_id"])
    frame = through_ball["freeze_frame"]
    second_last = sorted((p["location"][0] for p in frame if not p["teammate"]), reverse=True)[1]
    runner = next(p for p in frame if p["player_id"] == "home-20")["location"][0]
    assert 0.1 <= runner - second_last <= 0.4 and runner > through_ball["start_x"]


def test_freeze_frames(demo):
    stream, _ = demo
    reds = {"home": 0, "away": 0}
    for m in actions(stream):
        frame = m["freeze_frame"]
        assert len(frame) == 22 - reds["home"] - reds["away"]
        (actor,) = [p for p in frame if p["actor"]]
        assert actor["player_id"] == m["player"]["id"] and actor["teammate"]
        assert sum(p["keeper"] for p in frame) == 2
        if m.get("card") in ("red", "second_yellow"):
            reds[m["team"]] += 1


def test_planted_moments_outweigh_background_chances(demo):
    """A background shot never swings the result more than the planted 72' save: chance value = xG x shift of the result probabilities if it had gone in."""
    stream, truth = demo
    info = next(m for m in stream if m["type"] == "match_info")["teams"]
    model = SkellamWinProb(info["home"]["expected_goals"], info["away"]["expected_goals"])

    def value(shot):
        state = MatchState(shot["clock_s"] / 60, shot["score_before"]["home"], shot["score_before"]["away"], shot["reds_before"]["home"], shot["reds_before"]["away"], period=shot["period"])
        before, after = model.predict(state), model.predict(state.with_goal(shot["team"]))
        return shot["xg"] * 0.5 * (abs(after.home - before.home) + abs(after.draw - before.draw) + abs(after.away - before.away))

    save = next(s for s in truth["shots"] if s["planted"] == "save-72")
    background = [s for s in truth["shots"] if s["planted"] is None]
    assert background and max(value(s) for s in background) < value(save)


def test_envelope_and_clocks(matches, demo):
    for stream, _ in [*matches, demo]:
        assert all(all(k in m for k in ENVELOPE) for m in stream)
        assert [m["emitted_at_s"] for m in stream] == sorted(m["emitted_at_s"] for m in stream)
        msgs = in_order(stream)
        assert [m["seq"] for m in msgs] == list(range(1, len(msgs) + 1))
        assert msgs[0]["type"] == "match_info"
        walls = [m["wall_s"] for m in msgs]
        assert walls == sorted(walls)
        last_first_half = max(i for i, m in enumerate(stream) if m.get("period") == 1)
        first_second_half = min(i for i, m in enumerate(stream) if m.get("period") == 2)
        assert last_first_half < first_second_half, "the two halves interleave"
        for m in msgs[1:]:
            assert m["minute_label"] == minute_label(m["period"], m["clock_s"])
            assert m["period"] == 1 or m["clock_s"] >= 2700


def test_laws_hold(matches):
    for stream, truth in matches:
        acts = actions(stream)
        msgs = in_order(stream)
        gone: dict[str, int] = {}  # player id -> seq at which he left the pitch
        yellows: dict[str, int] = {}
        for m in msgs:
            if m["type"] == "substitution":
                gone[m["player_out"]["id"]] = m["seq"]
            if m["type"] == "action" and m["action_type"] == "foul" and m.get("card"):
                pid = m["player"]["id"]
                if m["card"] == "yellow":
                    yellows[pid] = yellows.get(pid, 0) + 1
                    assert yellows[pid] == 1, "a second yellow must be a red"
                else:
                    assert m["result"] == "red_card"
                    assert m["card"] == "red" or yellows.get(pid) == 1
                    gone[pid] = m["seq"]
        for m in acts:
            assert m["player"]["id"] not in gone or m["seq"] <= gone[m["player"]["id"]], "a player acts after leaving the pitch"
        for side in ("home", "away"):
            assert sum(m["type"] == "substitution" and m["team"] == side for m in msgs) <= 5 + 1  # a keeper sent off can bring on the substitute keeper
        voided = {m["target_event_id"] for m in msgs if m["type"] == "correction" and m["op"] == "void"}
        for i, m in enumerate(acts):
            nxt = next((n for n in acts[i + 1:] if n.get("restart")), None)
            if nxt is None or nxt["period"] != m["period"]:
                continue  # the half ended before the restart
            if m["action_type"].startswith("shot") and m["shot"]["outcome"] == "goal":
                other = "away" if m["team"] == "home" else "home"
                expected = "free_kick" if m["event_id"] in voided else "kick_off"
                assert nxt is not None and (nxt["team"], nxt["restart"]) == (other, expected)
            if m["result"] == "offside":
                assert (nxt["team"], nxt["restart"]) != (m["team"], "kick_off") and nxt["team"] != m["team"]
            if m["action_type"] == "shot" and m["shot"]["outcome"] == "off_target":
                assert nxt["restart"] == "goal_kick" and nxt["team"] != m["team"]
        for prev, m in zip(acts, acts[1:]):
            if prev["result"] == "success" and prev["action_type"] in ("pass", "dribble") and m["team"] == prev["team"] and not m.get("restart"):
                assert math.dist((prev["end_x"], prev["end_y"]), (m["start_x"], m["start_y"])) < 0.2, "the ball jumped"
        score = {"home": 0, "away": 0}
        for m in acts:
            if m["action_type"].startswith("shot") and m["shot"]["outcome"] == "goal" and m["event_id"] not in voided:
                score[m["team"]] += 1
        assert score == truth["final_score"] == next(m for m in msgs if m["type"] == "full_time")["score"]


def test_corrections_are_typed_and_ordered(matches, demo):
    for stream, truth in [*matches, demo]:
        msgs = in_order(stream)
        seq_of = {m["event_id"]: m["seq"] for m in msgs if m["type"] == "action"}
        arrival = {m["message_id"]: i for i, m in reversed(list(enumerate(stream)))}
        version: dict[str, int] = {}
        for m in msgs:
            if m["type"] != "correction":
                continue
            assert m["op"] in ("void", "reassign", "update")
            assert seq_of[m["target_event_id"]] < m["seq"]
            assert arrival[m["target_event_id"]] < arrival[m["message_id"]], "a correction arrives before its action"
            assert m["target_version"] == version.get(m["target_event_id"], 1) and m["new_version"] == m["target_version"] + 1
            version[m["target_event_id"]] = m["new_version"]
        assert len(truth["corrections"]) == sum(m["type"] == "correction" for m in msgs)


def test_calibration_against_real_orders_of_magnitude(matches):
    summary = summarise([truth["counts"] for _, truth in matches])
    off = {name: round(summary[name]["mean"], 2) for name, (low, high, _) in TARGETS.items() if not low <= summary[name]["mean"] <= high}
    assert not off, f"means outside their band over {len(SEEDS)} matches: {off}"


def test_replay_paces_key_moments(demo):
    stream, truth = demo
    times = schedule(stream, speed=10)
    at = {m["message_id"]: t for m, t in zip(stream, times)}
    goal = next(m["event_id"] for m in truth["moments"] if m["key"] == "goal-63")
    void = next(m for m in stream if m["type"] == "correction" and m["op"] == "void")
    assert at[void["message_id"]] - at[goal] >= 60, "the VAR check must play in real time, so the card can air before the retraction"
    first_half_end = next(t for m, t in zip(stream, times) if m["type"] == "period_end" and m["period"] == 1)
    second_half_start = next(t for m, t in zip(stream, times) if m["type"] == "period_start" and m["period"] == 2)
    assert second_half_start - first_half_end <= 15
    assert times[-1] < stream[-1]["emitted_at_s"] / 3
    late_start = schedule(stream, speed=10, from_minute=60)
    assert all(t == 0 for m, t in zip(stream, late_start) if m.get("clock_s", 0) < 3600 and m["emitted_at_s"] < 3600)
    sent = []
    replay(stream, sent.append, speed=0)
    assert sent == stream


def test_cdf_export_passes_the_official_validator(demo, tmp_path):
    cdf = pytest.importorskip("cdf")
    stream, truth = demo
    paths = {p.name.split(".")[1]: p for p in write_cdf(stream, tmp_path)}
    validators = {"meta": cdf.MetaSchemaValidator, "match": cdf.MatchSchemaValidator, "event": cdf.EventSchemaValidator, "tracking": cdf.TrackingSchemaValidator}
    for name, validator in validators.items():
        validator().validate_schema(sample=str(paths[name]), mode="extreme", limit=None)  # raises on any error, unknown keys included
    sheet = json.loads(paths["match"].read_text(encoding="utf-8"))
    final = sheet["match"]["result"]["final"]
    assert (final["home"], final["away"]) == (truth["final_score"]["home"], truth["final_score"]["away"])
    scorers = [g["player_id"] for g in sheet["events"]["goals"]]
    assert scorers.count("home_20") == 4 and "away_7" in scorers, "corrections are applied: the 31' goal belongs to #7, disallowed goals are gone"
    assert [c["type"] for c in sheet["events"]["cards"] if c["player_id"] == "away_5"] == ["yellow_card", "second_yellow_card"]
