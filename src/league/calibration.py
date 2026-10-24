"""Season-level calibration: many independent seasons, each with freshly drawn strengths, against real Premier League orders of magnitude."""

import numpy as np

from league.model import N_CLUBS, Params, draw_strengths, rates, sample_scores
from league.table import ranks, round_robin, standings

# name: (target, low, high, reference). The references are recalled figures, not yet checked against a published table.
TARGETS: dict[str, tuple[float, float, float, str]] = {
    "champion_points": (88.0, 84.0, 93.0, "87.6 on average, 1995/96 to 2024/25; 92.6 from 2016/17"),
    "eighteenth_points": (30.0, 26.0, 34.0, "about 30 on average, 2019/20 to 2024/25"),
    "points_sd": (18.5, 16.0, 21.0, "15 to 21 depending on the season"),
    "draw_rate": (0.24, 0.21, 0.27, "21 to 25 %"),
    "home_win_rate": (0.44, 0.41, 0.48, "41 to 48 %, 2020/21 excluded"),
    "points_per_goal_difference": (0.56, 0.50, 0.65, "about 0.6 point per goal of goal difference"),
}


def simulate_seasons(n_seasons: int, seed: int, params: Params = Params()) -> dict[str, np.ndarray]:
    """Final tables of `n_seasons` independent seasons, arrays of shape (n_seasons, 20), plus the scores of every fixture."""
    rng = np.random.default_rng(seed)
    fixtures = round_robin(N_CLUBS).reshape(-1, 2)
    home, away = fixtures[:, 0], fixtures[:, 1]
    attack, defence = draw_strengths(rng, params, (n_seasons,))
    hg, ag = sample_scores(rng, *rates(params, attack, defence, home, away), params.rho)
    t = standings(home, away, hg, ag, N_CLUBS)
    t["rank"] = ranks(t["points"], t["goal_difference"], t["goals_for"])
    t["home_goals"], t["away_goals"] = hg, ag
    t["attack"], t["defence"] = attack, defence
    return t


def summarise(t: dict[str, np.ndarray]) -> dict[str, float]:
    pts, gd, rank = t["points"].astype(float), t["goal_difference"].astype(float), t["rank"]
    by_rank = np.take_along_axis(pts, np.argsort(rank, axis=-1), axis=-1)
    gd_c = gd - gd.mean(axis=-1, keepdims=True)
    slope = (gd_c * (pts - pts.mean(axis=-1, keepdims=True))).sum(axis=-1) / (gd_c**2).sum(axis=-1)
    hg, ag = t["home_goals"], t["away_goals"]
    strength = t["attack"] - t["defence"]
    return {
        "champion_points": float(by_rank[:, 0].mean()),
        "eighteenth_points": float(by_rank[:, 17].mean()),
        "points_sd": float(pts.std(axis=-1).mean()),
        "draw_rate": float((hg == ag).mean()),
        "home_win_rate": float((hg > ag).mean()),
        "points_per_goal_difference": float(slope.mean()),
        "goals_per_match": float((hg + ag).mean()),
        "clubs_80_or_more": float((pts >= 80).sum(axis=-1).mean()),
        "clubs_30_or_less": float((pts <= 30).sum(axis=-1).mean()),
        "seasons_one_95": float(((pts >= 95).sum(axis=-1) >= 1).mean()),
        "seasons_two_95": float(((pts >= 95).sum(axis=-1) >= 2).mean()),
        "strongest_club_champion": float((np.take_along_axis(rank, strength.argmax(axis=-1)[:, None], axis=-1) == 1).mean()),
    }


def calibrate(n_seasons: int = 1000, seed: int = 1, params: Params = Params()) -> dict[str, float]:
    return summarise(simulate_seasons(n_seasons, seed, params))
