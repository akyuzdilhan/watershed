import json
import pytest

from generator import generate_match
from generator.publish import load
from worker.evaluate import DEMO_REPLAY, DEMO_TRUTH, PERTURBATIONS, arrivals, bench_summary, diff, evaluate, evaluate_match, perturb, render, run, snapshot, to_json

SEEDS = (1, 2, 3)


@pytest.fixture(scope="module")
def matches():
    return [evaluate_match(*generate_match(seed, frames=False), robustness=False) for seed in SEEDS]


@pytest.fixture(scope="module")
def demo():
    return evaluate_match(load(DEMO_REPLAY), json.loads(DEMO_TRUTH.read_text(encoding="utf-8")))


def test_unscripted_matches_score_perfectly_on_goals_and_actions(matches):
    for m in matches:
        assert m["goals_found"] == m["goals"] and m["reds_found"] == m["reds"], m["match_id"]
        assert all(k.split("|")[0] == k.split("|")[1] for k in m["confusion"]), m["confusion"]
        assert not m["false_retractions"] and not m["false_rewrites"] and m["score_ok"]
        assert m["writes"] and m["writes_verified"] == m["writes"]
        assert sum(m["card_class"].values()) == m["cards"]


def test_demo_finds_the_eleven_moments_and_times_the_63rd_minute_card(demo):
    assert demo["cards"] == 11 and demo["card_class"] == {"truth_goal": 9, "truth_red": 1, "truth_save": 1}
    assert demo["confusion"] == {"none|none": 6, "retract|retract": 2, "rewrite|rewrite": 1}
    w = demo["demo"]["goal-63"]
    assert 0 < w["hold_after_card_s"] < w["retract_after_card_s"]  # the card can go on air, is held during the VAR check, then retracted
    assert w["var_duration_s"] == pytest.approx(70.0)


def test_demo_is_identical_under_random_network_trouble(demo):
    assert all(not d for name, d in demo["robustness"].items() if name != "goal_after_correction"), demo["robustness"]


@pytest.mark.xfail(reason="Match._apply drops a reassign or update whose target action has not arrived yet, and the Spotter cards a goal that is already voided (docs/notes/evaluation.md)", strict=False)
def test_a_goal_that_arrives_after_its_correction_still_gets_the_right_card_action(demo):
    assert demo["robustness"]["goal_after_correction"] == []


def test_the_targeted_perturbation_skips_matches_without_a_corrected_goal():
    for seed in SEEDS:
        stream, truth = generate_match(seed, frames=False)
        corrected = any(g["expected_card_action"] != "none" for g in truth["goals"])
        assert (perturb(stream, "goal_after_correction", seed) is None) == (not corrected)


@pytest.mark.parametrize("name", [n for n, p in PERTURBATIONS.items() if p["within"]])
def test_perturbations_within_the_buffer_change_nothing(name):
    stream, truth = generate_match(2, frames=False)
    assert diff(snapshot(run(arrivals(stream), desk=False)), snapshot(run(perturb(stream, name, 2), desk=False))) == []


def test_perturb_is_deterministic_and_only_moves_or_repeats_messages():
    stream, _ = generate_match(1, frames=False)
    a, b = perturb(stream, "combined", 1), perturb(stream, "combined", 1)
    assert [(t, m["message_id"]) for t, m in a] == [(t, m["message_id"]) for t, m in b]
    assert {m["message_id"] for _, m in a} == {m["message_id"] for m in stream}
    assert len(a) > len(stream) and all(t >= m["emitted_at_s"] for t, m in a)
    assert [t for t, _ in a] == sorted(t for t, _ in a)


def test_the_report_is_deterministic():
    r1, r2 = evaluate(range(1, 3), demo=False, robustness=False), evaluate(range(1, 3), demo=False, robustness=False)
    assert render(r1) == render(r2) and to_json(r1) == to_json(r2)
    assert "## 1. Moments and cards" in render(r1) and json.loads(to_json(r1))["unscripted"]["matches"] == 2


@pytest.mark.xfail(reason="Spotter.saves keeps a save waiting for any later keeper action: a late shot after its keeper save is credited to a save 46 minutes later (docs/notes/evaluation.md)", strict=False)
def test_a_save_card_credits_the_keeper_action_right_after_its_shot():
    m = evaluate_match(*generate_match(53, frames=False), robustness=False)
    assert "mispaired_save" not in m["card_class"]


def test_bench_summary_reads_jsonl_without_calling_a_model(tmp_path):
    row = {"model": "m", "effort": "low", "case": "save-en-fr", "rep": 0, "seconds": 4.0, "error": None, "report": {"json_valid": True, "schema_ok": True, "verified": True, "cards": [{"verified": True}, {"verified": False}]}}
    trap = {**row, "case": "trap-x", "seconds": 2.0, "report": {"json_valid": True, "schema_ok": True, "verified": True, "cards": [{"verified": True}]}}
    (tmp_path / "m_low.jsonl").write_text("\n".join(json.dumps(r) for r in (row, trap)) + "\n", encoding="utf-8")
    (s,) = bench_summary(tmp_path)
    assert (s["runs"], s["cards_verified"], s["cards_expected"], s["traps_held"], s["seconds_per_card_median"]) == (2, 2, 3, 1, 2.0)
    assert bench_summary(tmp_path / "missing") == []
