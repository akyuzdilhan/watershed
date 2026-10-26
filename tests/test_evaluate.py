import json
from pathlib import Path

import pytest

from generator import generate_match
from generator.publish import load
from worker.evaluate import (DEMO_REPLAY, DEMO_TRUTH, PERTURBATIONS, arrivals, begin_marker, bench_summary, diff, end_marker, evaluate, evaluate_match, perturb,
                             render, render_blocks, run, snapshot, splice, to_json)

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


def test_a_goal_that_arrives_after_its_correction_ends_with_the_right_cards():
    """When a correction overtakes its goal, the card is born corrected: a voided goal gets no card (nothing to retract), a reassigned goal gets the right scorer at once. What a viewer could see must be the same."""
    stream, truth = generate_match(7, "demo", frames=False)

    def on_air(result):
        cards = snapshot(result)["cards"]
        return {cid: (c[0], c[3], c[4]) for cid, c in cards.items() if c[1] != "retracted"}  # moment, team, player

    assert on_air(run(arrivals(stream), desk=False)) == on_air(run(perturb(stream, "goal_after_correction", 7), desk=False))


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


def test_the_written_file_keeps_what_was_written_by_hand():
    blocks = {"report": "# Evaluation\n\nnew numbers", "bench": "| new bench |", "timing": None}
    fresh = splice(None, blocks)
    assert fresh.startswith(begin_marker("report")) and "new numbers" in fresh and "| new bench |" in fresh and "timing" not in fresh
    edited = (fresh.replace("### Earlier model benchmark", "### Spike prompt benchmark, 23/10").replace("new numbers", "old numbers")
              .replace("| new bench |", "| old bench |") + "\n## Live run, production prompt\n\nWritten by hand.\n")
    edited = edited.replace(end_marker("bench") + "\n", end_marker("bench") + "\n\n" + begin_marker("timing") + "\nstale times\n" + end_marker("timing") + "\n")
    out = splice(edited, blocks)
    assert "new numbers" in out and "old numbers" not in out and "| new bench |" in out
    assert "### Spike prompt benchmark, 23/10" in out and out.endswith("## Live run, production prompt\n\nWritten by hand.\n")
    assert "stale times" not in out and begin_marker("timing") not in out  # the times of an earlier run are dropped
    assert splice(out, blocks) == out
    kept = splice(edited, {**blocks, "bench": None})  # no benchmark file on this machine: the table in the file stays
    assert "| old bench |" in kept
    timed = splice(out, {**blocks, "timing": "## 6. Processing time"})
    assert timed.rstrip().endswith(end_marker("timing"))
    with pytest.raises(ValueError):
        splice("# Evaluation written by hand, no markers\n", blocks)


def test_the_committed_report_has_its_generated_regions_and_its_hand_written_sections():
    text = Path("docs/evaluation.md").read_text(encoding="utf-8")
    for name in ("report", "bench"):
        assert text.count(begin_marker(name)) == 1 and text.count(end_marker(name)) == 1
    order = [text.index(end_marker("report")), text.index("### Spike prompt benchmark"), text.index(begin_marker("bench")), text.index("## Live run, production prompt")]
    assert order == sorted(order)


def test_render_is_the_new_file_without_its_markers():
    report = evaluate(range(1, 2), demo=False, robustness=False)
    blocks = render_blocks(report)
    assert blocks["bench"] is None and blocks["timing"] is None
    stripped = "\n".join(line for line in splice(None, blocks).split("\n") if not line.startswith("<!-- generated by"))
    assert stripped == render(report)
