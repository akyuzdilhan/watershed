from dataclasses import replace

import numpy as np
import pytest
from scipy.stats import poisson

from winprob.model import MatchState, WinProb, WinProbModel
from winprob.skellam import SkellamWinProb
from winprob.table import TableWinProb

EVEN = SkellamWinProb(home_strength=1.35, away_strength=1.35)
MODELS: dict[str, WinProbModel] = {"skellam": SkellamWinProb(), "table": TableWinProb.from_model(SkellamWinProb())}
EVEN_MODELS: dict[str, WinProbModel] = {"skellam": EVEN, "table": TableWinProb.from_model(EVEN)}
# States before full time, with goal differences small enough for one more goal to stay inside the table.
LIVE = [MatchState(m, h, a, hr, ar) for m in (0, 23, 45.5, 67, 81, 89) for h, a in [(0, 0), (1, 0), (1, 1), (0, 2), (3, 1)] for hr, ar in [(0, 0), (1, 0), (0, 1)]]


def mirror(s: MatchState) -> MatchState:
    return MatchState(s.minute, s.away_goals, s.home_goals, s.away_reds, s.home_reds)


@pytest.mark.parametrize("name", MODELS)
def test_probabilities_sum_to_one(name):
    for s in LIVE + [MatchState(95, 1, 0), MatchState(101.5, 2, 2, away_reds=2)]:
        p = MODELS[name].predict(s)
        assert p.home + p.draw + p.away == pytest.approx(1.0, abs=1e-12)
        assert min(p.home, p.draw, p.away) >= 0
    with pytest.raises(ValueError):
        WinProb(0.5, 0.5, 0.5)


@pytest.mark.parametrize("name", EVEN_MODELS)
def test_even_strengths_are_symmetric(name):
    for s in LIVE:
        p, q = EVEN_MODELS[name].predict(s), EVEN_MODELS[name].predict(mirror(s))
        assert (p.home, p.draw, p.away) == pytest.approx((q.away, q.draw, q.home), abs=1e-12)


@pytest.mark.parametrize("name", MODELS)
def test_a_goal_raises_the_scorers_win_probability(name):
    model = MODELS[name]
    for s in LIVE:
        before = model.predict(s)
        assert model.predict(s.with_goal("home")).home > before.home
        assert model.predict(s.with_goal("away")).away > before.away


def test_a_red_card_lowers_the_teams_win_probability():
    model = SkellamWinProb()
    for s in LIVE:
        before = model.predict(s)
        assert model.predict(replace(s, home_reds=s.home_reds + 1)).home < before.home
        assert model.predict(replace(s, away_reds=s.away_reds + 1)).away < before.away


def test_red_cards_scale_the_rates():
    # Each red card multiplies the carded team's rate by red_own and its opponent's by red_opponent.
    model = SkellamWinProb(home_strength=1.6, away_strength=1.1, red_own=0.5, red_opponent=2.0)
    for hr, ar in [(1, 0), (0, 1), (2, 1)]:
        scaled = SkellamWinProb(home_strength=1.6 * 0.5**hr * 2.0**ar, away_strength=1.1 * 0.5**ar * 2.0**hr)
        p, q = model.predict(MatchState(67, 1, 1, hr, ar)), scaled.predict(MatchState(67, 1, 1))
        assert (p.home, p.draw, p.away) == pytest.approx((q.home, q.draw, q.away), abs=1e-12)


@pytest.mark.parametrize("name", MODELS)
def test_converges_to_the_result_at_full_time(name):
    model = MODELS[name]
    leader = [model.predict(MatchState(m, 2, 1)).home for m in (60, 80, 90, 94, 94.9)]
    assert all(a < b for a, b in zip(leader, leader[1:])) and leader[-1] > 0.99
    assert model.predict(MatchState(95, 2, 1)) == WinProb(1.0, 0.0, 0.0)
    assert model.predict(MatchState(95, 1, 1)) == WinProb(0.0, 1.0, 0.0)
    # Past the expected duration it is still full time.
    assert model.predict(MatchState(97.5, 0, 2, home_reds=1)) == WinProb(0.0, 0.0, 1.0)


def test_duration_is_a_parameter():
    # Half the match left under both durations; full time comes at the chosen duration, not at 95.
    p, q = SkellamWinProb(duration=100).predict(MatchState(50, 1, 0)), SkellamWinProb().predict(MatchState(47.5, 1, 0))
    assert (p.home, p.draw, p.away) == pytest.approx((q.home, q.draw, q.away), abs=1e-12)
    assert SkellamWinProb(duration=100).predict(MatchState(97.5, 1, 0)).home < 1
    assert SkellamWinProb(duration=90).predict(MatchState(92, 1, 0)) == WinProb(1.0, 0.0, 0.0)
    assert TableWinProb.from_model(SkellamWinProb(duration=100)).predict(MatchState(100, 2, 1)) == WinProb(1.0, 0.0, 0.0)


def test_reference_case_rovers_united_at_67():
    # Rovers 1-1 United at 67' (the key save of the demo match), close strengths.
    wp = SkellamWinProb(home_strength=1.4, away_strength=1.3).predict(MatchState(67, 1, 1))
    assert 0.20 < wp.away < 0.40


def test_skellam_matches_counting_poisson_goals():
    # Same result as enumerating every pair of goal counts for the time left.
    model = SkellamWinProb(home_strength=1.6, away_strength=1.1)
    goals = np.arange(40)
    for minute, home, away in [(0, 0, 0), (30, 0, 1), (67, 1, 1), (88, 2, 0)]:
        left = (95 - minute) / 95
        joint = np.outer(poisson.pmf(goals, 1.6 * left), poisson.pmf(goals, 1.1 * left))
        final = home - away + goals[:, None] - goals[None, :]
        wp = model.predict(MatchState(minute, home, away))
        assert (wp.home, wp.draw, wp.away) == pytest.approx((joint[final > 0].sum(), joint[final == 0].sum(), joint[final < 0].sum()), abs=1e-12)


def test_parameters_must_be_finite_and_positive():
    for bad in ({"home_strength": 0}, {"red_opponent": float("nan")}, {"duration": float("inf")}):
        with pytest.raises(ValueError):
            SkellamWinProb(**bad)


def test_table_takes_its_outermost_column_beyond_max_diff():
    model = SkellamWinProb()
    table = TableWinProb.from_model(model, max_diff=2)
    for d in (2, 3, 6):
        assert table.predict(MatchState(60, d, 0)) == model.predict(MatchState(60, 2, 0))
        assert table.predict(MatchState(60, 0, d)) == model.predict(MatchState(60, 0, 2))


def test_with_goal_is_the_counterfactual_state():
    s = MatchState(67, 1, 1, home_reds=1)
    assert s.with_goal("home") == MatchState(67, 2, 1, home_reds=1)
    assert s.with_goal("away") == MatchState(67, 1, 2, home_reds=1)
    assert s == MatchState(67, 1, 1, home_reds=1)
    with pytest.raises(ValueError):
        s.with_goal("Rovers")
