"""Club layer of the league history: a pool of 32 fictional clubs, strengths that drift between seasons, two tiers at score level, promotion and relegation.

Each club has a latent quality q (log scale, as `league.model`): q = mean + regime + an AR(1) deviation. The mean is fixed per club, a regime (a boom or a decline: a new owner, a golden generation, a financial crash) starts with a small chance each season and fades slowly, and the AR(1) term carries form from one season to the next. Attack and solidity are q plus a per-season style noise, which gives the league model's correlation of about 0.8 between them.

Names are given after the simulation: the 20 clubs of the season after the last one are matched by strength rank to the 20 clubs of `League.create(seed)` (the season of the demo match), so the club that the league makes strongest in 2026/27 is also the strongest club entering it in the history.
"""

from dataclasses import dataclass

import numpy as np

from league.clubs import CLUBS, DEMO_AWAY, DEMO_HOME
from league.model import sample_scores
from league.season import League
from league.table import ranks, round_robin, standings

# Twelve more invented names; none shares a word with a current Premier League club.
EXTRA_CLUBS: tuple[str, ...] = (
    "Ambleforth", "Brindlecombe", "Cresswater", "Drayhaven", "Easterholm", "Foxmarsh",
    "Gorsedale", "Hethermoor Borough", "Kingsmere Athletic", "Lowbridge", "Netherwick", "Sallowford",
)
POOL = len(CLUBS) + len(EXTRA_CLUBS)  # 32
TOP_CLUBS, SECOND_CLUBS, MOVERS = 20, POOL - 20, 3
LAST_YEAR = 2025  # the last season of the history starts in 2025: 2025/26

# Strength dynamics (log scale), hand-set so that 50 seasons give dynasties, slumps and a calibrated top tier (docs/notes/history.md).
BASE_SD = 0.22  # spread of the clubs' long-run means
REGIME_P = 0.06  # chance per season that a club enters a boom or a decline
REGIME_SD = 0.28  # size of a new regime
REGIME_DECAY = 0.88  # a regime fades by this factor each season
PHI = 0.70  # AR(1) persistence of the season-to-season deviation
EPS_SD = 0.13  # innovation of the AR(1) deviation
STYLE_SD = 0.13  # per-season attack and solidity noise around q
RHO = -0.10  # Dixon-Coles, as league.model

# Era shapes (start year of the season, value), linear in between. Rough shapes, recalled and not sourced.
GOALS = ((1976, 2.70), (1990, 2.60), (2002, 2.55), (2012, 2.78), (2025, 2.88))
SECOND_TIER_GOALS = 2.60
HOME_RATIO = ((1976, 1.45), (2000, 1.32), (2025, 1.22))


def era(points: tuple[tuple[int, float], ...], year: int) -> float:
    xs, ys = zip(*points)
    return float(np.interp(year, xs, ys))


def season_label(year: int) -> str:
    return f"{year}/{(year + 1) % 100:02d}"


@dataclass
class ClubLayer:
    years: list[int]  # start year of each season
    names: list[str]  # club index -> name
    founded: list[int]  # founding year of each club
    q: np.ndarray  # (n_seasons + 1, POOL): latent quality, the extra row is the season after the last one
    tier: np.ndarray  # (n_seasons, POOL): 1 or 2
    rank: np.ndarray  # (n_seasons, POOL): position within the tier
    table: dict[str, np.ndarray]  # (n_seasons, POOL) per field: played, won, drawn, lost, goals_for, goals_against, points
    fixtures: list[dict[str, np.ndarray]]  # per season, top tier: matchday, home, away, home_goals, away_goals (club indices)


def _rates(attack: np.ndarray, defence: np.ndarray, home: np.ndarray, away: np.ndarray, goals: float, home_ratio: float) -> tuple[np.ndarray, np.ndarray]:
    """Expected goals of fixtures, with mu set so that a full round robin of these clubs averages `goals` a match."""
    h = np.log(home_ratio)
    n = attack.size
    a, d = attack[:, None], defence[None, :]
    total = np.exp(h + a + d) + np.exp(a + d).T  # [i, j]: i at home to j, both sides
    mask = ~np.eye(n, dtype=bool)
    mu = np.log(goals * mask.sum() / total[mask].sum())
    return np.exp(mu + h + attack[home] + defence[away]), np.exp(mu + attack[away] + defence[home])


def _play(rng: np.random.Generator, clubs: np.ndarray, q: np.ndarray, goals: float, home_ratio: float) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """One double round robin between `clubs` (pool indices): fixtures with pool indices, and the table per position in `clubs`."""
    n = clubs.size
    attack = q[clubs] + STYLE_SD * rng.standard_normal(n)
    defence = -(q[clubs] + STYLE_SD * rng.standard_normal(n))
    sched = round_robin(n, rng)  # (matchdays, n/2, 2), local indices
    home, away = sched[..., 0].ravel(), sched[..., 1].ravel()
    lam_h, lam_a = _rates(attack, defence, home, away, goals, home_ratio)
    hg, ag = sample_scores(rng, lam_h, lam_a, RHO)
    t = standings(home, away, hg, ag, n)
    t["rank"] = ranks(t["points"], t["goal_difference"], t["goals_for"])
    matchday = np.repeat(np.arange(1, sched.shape[0] + 1), sched.shape[1])
    fx = {"matchday": matchday, "home": clubs[home], "away": clubs[away], "home_goals": hg, "away_goals": ag}
    return fx, t


STORY_ATTEMPTS = 40
STORY_MIN_SEASONS = 0.4  # share of the seasons each demo club must have spent in the top tier


def simulate_clubs(seed: int, n_seasons: int) -> ClubLayer:
    """Strengths, tables and top-tier scores of `n_seasons` seasons ending in 2025/26, for the history of `seed`.

    Deterministic search for a history that serves the demo: the first attempt where Rovers and United have each spent at least 40 % of the seasons in the top tier and have won at least one title between them (over 10 seasons or more). Falls back to the last attempt.
    """
    for attempt in range(STORY_ATTEMPTS):
        layer = _simulate(seed, n_seasons, attempt)
        if story_ok(layer) or attempt == STORY_ATTEMPTS - 1:
            return layer
    raise AssertionError("unreachable")


def story_ok(layer: "ClubLayer") -> bool:
    n = len(layer.years)
    titles = 0
    for demo in (DEMO_HOME, DEMO_AWAY):
        c = layer.names.index(demo)
        if (layer.tier[:, c] == 1).sum() < STORY_MIN_SEASONS * n:
            return False
        titles += int(((layer.tier[:, c] == 1) & (layer.rank[:, c] == 1)).sum())
    return titles >= 1 or n < 10


def _simulate(seed: int, n_seasons: int, attempt: int) -> ClubLayer:
    years = list(range(LAST_YEAR - n_seasons + 1, LAST_YEAR + 1))
    r_dyn, r_play, r_names = (np.random.default_rng(s) for s in np.random.SeedSequence([seed, 7677, attempt]).spawn(3))
    base = BASE_SD * r_dyn.standard_normal(POOL)
    regime = np.where(r_dyn.random(POOL) < 0.3, REGIME_SD * r_dyn.standard_normal(POOL), 0.0)
    dev = EPS_SD / np.sqrt(1 - PHI**2) * r_dyn.standard_normal(POOL)
    q = np.empty((n_seasons + 1, POOL))
    q[0] = base + regime + dev
    for s in range(1, n_seasons + 1):
        new = r_dyn.random(POOL) < REGIME_P
        regime = np.where(new, REGIME_SD * r_dyn.standard_normal(POOL), REGIME_DECAY * regime)
        dev = PHI * dev + EPS_SD * r_dyn.standard_normal(POOL)
        q[s] = base + regime + dev

    top = np.sort(np.argsort(-(q[0] + 0.1 * r_dyn.standard_normal(POOL)))[:TOP_CLUBS])
    tier = np.zeros((n_seasons, POOL), np.int64)
    rank = np.zeros((n_seasons, POOL), np.int64)
    fields = ("played", "won", "drawn", "lost", "goals_for", "goals_against", "points")
    table = {k: np.zeros((n_seasons, POOL), np.int64) for k in fields}
    fixtures = []
    for s, year in enumerate(years):
        second = np.setdiff1d(np.arange(POOL), top)
        fx, t1 = _play(r_play, top, q[s], era(GOALS, year), era(HOME_RATIO, year))
        _, t2 = _play(r_play, second, q[s], SECOND_TIER_GOALS, era(HOME_RATIO, year))
        for clubs, t, level in ((top, t1, 1), (second, t2, 2)):
            tier[s, clubs] = level
            rank[s, clubs] = t["rank"]
            for k in fields:
                table[k][s, clubs] = t[k]
        fixtures.append(fx)
        down = top[t1["rank"] > TOP_CLUBS - MOVERS]
        up = second[t2["rank"] <= MOVERS]
        top = np.sort(np.concatenate([np.setdiff1d(top, down), up]))

    names = _names(seed, q[n_seasons], top, tier[-1], r_names)
    founded = [int(y) for y in r_names.integers(1865, 1913, POOL)]
    return ClubLayer(years, names, founded, q, tier, rank, table, fixtures)


def _names(seed: int, q_next: np.ndarray, top_next: np.ndarray, tier_last: np.ndarray, rng: np.random.Generator) -> list[str]:
    """Name the latent clubs: the 20 clubs of the next season by strength rank against League.create(seed), the others with the extra names.

    Rovers and United must have played the last season in the top tier (their head-to-head and the demo players' last season are then on record): if either is matched to a promoted club, it swaps names with the closest-ranked club that stayed up.
    """
    league = League.create(seed)
    overall = league.attack - league.defence
    by_league = [CLUBS[i] for i in np.argsort(-overall)]
    by_history = list(top_next[np.argsort(-q_next[top_next])])
    names: dict[int, str] = {int(c): n for c, n in zip(by_history, by_league)}
    stayed = [int(c) for c in by_history if tier_last[c] == 1]
    for demo in (DEMO_HOME, DEMO_AWAY):
        c = next(k for k, v in names.items() if v == demo)
        if tier_last[c] == 1:
            continue
        pos = by_history.index(c)
        other = min((k for k in stayed if names[k] not in (DEMO_HOME, DEMO_AWAY)), key=lambda k: abs(by_history.index(k) - pos))
        names[c], names[other] = names[other], names[c]
    rest = [c for c in range(POOL) if c not in names]
    for c, n in zip(rest, rng.permutation(np.array(EXTRA_CLUBS)).tolist()):
        names[c] = n
    return [names[c] for c in range(POOL)]
