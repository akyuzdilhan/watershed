import itertools

import numpy as np
import pytest
from scipy.stats import poisson

from generator.engine import MatchEngine
from league.calibration import TARGETS, calibrate
from league.clubs import CLUBS
from league.coupling import demo_fixture
from league.model import Params, dc_tau, sample_scores
from league.season import MATCHDAYS, League, match_importance, season_state
from league.table import order, ranks, round_robin, standings

SIMS = 1000  # enough for sums and bounds, fast enough for the suite


@pytest.fixture(scope="module")
def league() -> League:
    return League.create(1)


def test_reproducible():
    a, b = League.create(5), League.create(5)
    for name in ("attack", "defence", "prior_attack", "prior_defence", "schedule", "home_goals", "away_goals"):
        assert np.array_equal(getattr(a, name), getattr(b, name))
    assert not np.array_equal(a.home_goals, League.create(6).home_goals)
    assert calibrate(50, seed=3) == calibrate(50, seed=3)
    assert season_state(a, 20, SIMS) == season_state(b, 20, SIMS)


def test_demo_matchday_only_reorders_the_calendar():
    a, b = League.create(2, demo_matchday=30), League.create(2, demo_matchday=12)
    assert np.array_equal(a.attack, b.attack)

    def results(lg):
        return sorted(zip(lg.schedule[..., 0].ravel(), lg.schedule[..., 1].ravel(), lg.home_goals.ravel(), lg.away_goals.ravel()))

    assert results(a) == results(b)
    for lg, md in ((a, 30), (b, 12)):
        fixtures = lg.schedule[md - 1].tolist()
        assert [CLUBS.index("Rovers"), CLUBS.index("United")] in fixtures


def test_calendar_is_a_double_round_robin():
    s = round_robin(20, np.random.default_rng(0))
    assert s.shape == (38, 10, 2)
    for day in s:
        assert sorted(day.ravel()) == list(range(20))  # every club plays once a matchday
    pairs = [tuple(p) for p in s.reshape(-1, 2)]
    assert sorted(pairs) == sorted(itertools.permutations(range(20), 2))  # each club hosts each other club once


@pytest.mark.parametrize("elite", [False, True])
def test_calibration_over_1000_seasons(elite):
    summary = calibrate(1000, seed=1, params=Params(elite=elite))
    for name, (_, low, high, _) in TARGETS.items():
        assert low <= summary[name] <= high, (name, summary[name])
    assert summary["goals_per_match"] == pytest.approx(2.85, abs=0.06), "the Jensen correction keeps the league average on target"
    if elite:
        assert summary["champion_points"] > calibrate(1000, seed=1)["champion_points"] + 2
        assert summary["seasons_two_95"] > 0.02


def test_dixon_coles_rejection_sampling_matches_the_corrected_pmf():
    lam, mu, rho = 1.4, 1.1, -0.10
    x, y = sample_scores(np.random.default_rng(0), np.full(200_000, lam), np.full(200_000, mu), rho)
    goals = np.arange(15)
    gx, gy = np.meshgrid(goals, goals, indexing="ij")
    pmf = np.outer(poisson.pmf(goals, lam), poisson.pmf(goals, mu)) * dc_tau(gx, gy, np.full(gx.shape, lam), np.full(gx.shape, mu), rho)
    pmf /= pmf.sum()
    for i, j in [(0, 0), (1, 0), (0, 1), (1, 1), (2, 1)]:
        assert np.mean((x == i) & (y == j)) == pytest.approx(pmf[i, j], abs=0.004)
    assert np.mean(x == y) > np.mean(np.random.default_rng(1).poisson(lam, 200_000) == np.random.default_rng(2).poisson(mu, 200_000))


def test_points_and_tie_breakers():
    # 0 beats 1, 1 beats 2, 2 beats 0 (all 1-0), 3 draws with everyone: 3-1-0 gives 4, 4, 4 and 3 points.
    home, away = np.array([0, 1, 2, 3, 3, 3]), np.array([1, 2, 0, 0, 1, 2])
    t = standings(home, away, np.array([1, 1, 1, 0, 2, 0]), np.array([0, 0, 0, 0, 2, 0]), 4)
    assert t["points"].tolist() == [4, 4, 4, 3]
    assert t["played"].tolist() == [3, 3, 3, 3]
    assert t["goal_difference"].tolist() == [0, 0, 0, 0] and t["goals_for"].tolist() == [1, 3, 1, 2]
    # equal points: goal difference, then goals scored, then club index
    pts, gd, gf = np.array([10, 10, 10, 10, 12]), np.array([3, 5, 3, 3, -1]), np.array([7, 2, 9, 7, 1])
    assert order(pts, gd, gf).tolist() == [4, 1, 2, 0, 3]
    assert ranks(pts, gd, gf).tolist() == [4, 2, 3, 5, 1]


def test_probabilities_sum_and_bounds(league):
    for md in (0, 30, 37):
        rows = season_state(league, md, SIMS)["table"]
        assert [r["rank"] for r in rows] == list(range(1, 21))
        for name, total in (("title_probability", 1), ("top4_probability", 4), ("relegation_probability", 3)):
            values = [r[name] for r in rows]
            assert sum(values) == pytest.approx(total)
            assert all(0 <= v <= 1 for v in values)
        assert all(r["top4_probability"] >= r["title_probability"] for r in rows)
    final = season_state(league, MATCHDAYS, SIMS)["table"]
    assert [r["title_probability"] for r in final] == [1.0] + [0.0] * 19, "nothing left to play: the table is final"
    assert [r["relegation_probability"] for r in final] == [0.0] * 17 + [1.0] * 3


def test_points_gaps(league):
    rows = season_state(league, 30, SIMS)["table"]
    assert rows[0]["points_behind_leader"] == 0 and all(r["points_behind_leader"] >= 0 for r in rows)
    assert all(r["points_from_top4"] >= 0 for r in rows[:4]) and all(r["points_from_top4"] <= 0 for r in rows[4:])
    assert all(r["points_clear_of_drop_zone"] >= 0 for r in rows[:17]) and all(r["points_clear_of_drop_zone"] <= 0 for r in rows[17:])


def test_importance_sign(league):
    seen = 0
    for home, away in league.schedule[30].tolist():  # matchday 31, after 30 matchdays
        stakes = match_importance(league, CLUBS[home], CLUBS[away], 30, SIMS)
        for d in stakes.values():
            assert d["title_stake"] >= 0 and d["top4_stake"] >= 0 and d["relegation_stake"] <= 0
            seen += abs(d["relegation_stake"]) > 0.01 or d["title_stake"] > 0.01
    assert seen, "at least one match matters"
    with pytest.raises(ValueError):
        match_importance(league, CLUBS[home], CLUBS[away], 31, SIMS)  # already played


def test_observer_is_not_the_truth_and_learns(league):
    assert not np.allclose(league.prior_attack, league.attack)

    def error(md):
        return np.abs(league.observer(md)[0] - league.attack).mean()

    assert np.array_equal(league.observer(0)[0], league.prior_attack)
    assert error(38) < error(0)


def test_with_result_replaces_one_score(league):
    rovers, united = CLUBS.index("Rovers"), CLUBS.index("United")
    hit = (league.schedule[..., 0] == rovers) & (league.schedule[..., 1] == united)
    changed = league.with_result("Rovers", "United", 5, 2)
    assert changed.table(38)["goals_for"][rovers] - league.table(38)["goals_for"][rovers] == 5 - int(league.home_goals[hit][0])
    assert np.array_equal(changed.table(29)["points"], league.table(29)["points"]), "the demo fixture is on matchday 30"


def test_demo_fixture_coupling():
    # What the league would give the demo match on matchday 30 of seed 1. The demo itself is unchanged: it keeps 1.5 and 1.2.
    demo = demo_fixture(seed=1, matchday=30, n_sims=SIMS)
    assert demo["expected_goals"] == {"home": 0.98, "away": 1.79}  # true strengths: for generator.generate_match
    assert demo["prior_expected_goals"] == {"home": 1.25, "away": 2.26}  # observer: for the win-probability model
    c = demo["context"]
    assert (c["home_club"], c["home_rank"], c["home_points"], c["away_club"], c["away_rank"], c["away_points"]) == ("Rovers", 16, 28, "United", 3, 57)
    assert c["home_played"] == c["away_played"] == 29
    assert c["points_gap_home_minus_away"] == -29
    for key, value in c.items():
        if key.endswith("_pct"):
            assert isinstance(value, int) and 0 <= value <= 100, key
    assert c["home_relegation_stake_pct_points"] <= 0 and c["away_top4_stake_pct_points"] > 0
    teams = MatchEngine(7, "demo").state.teams
    assert (teams["home"].expected_goals, teams["away"].expected_goals) == (1.5, 1.2)
