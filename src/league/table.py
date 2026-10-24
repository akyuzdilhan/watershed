"""Fixtures and standings: double round robin, 3-1-0 points, Premier League tie-breakers. Vectorised over seasons or simulations."""

import numpy as np


def round_robin(n_clubs: int, rng: np.random.Generator | None = None) -> np.ndarray:
    """A double round robin by the circle method, shape (2 * (n - 1), n // 2, 2): matchday, match, (home, away).

    The second half mirrors the first with home and away swapped. With `rng`, clubs are shuffled first, so the calendar changes with the seed.
    """
    if n_clubs % 2:
        raise ValueError("an even number of clubs is needed")
    order = list(rng.permutation(n_clubs)) if rng is not None else list(range(n_clubs))
    rounds = []
    for r in range(n_clubs - 1):
        pairs = []
        for i in range(n_clubs // 2):
            a, b = order[i], order[n_clubs - 1 - i]
            pairs.append((a, b) if (r + i) % 2 == 0 else (b, a))
        rounds.append(pairs)
        order = [order[0], order[-1], *order[1:-1]]
    first = np.array(rounds, dtype=np.int64)
    return np.concatenate([first, first[..., ::-1]])


def pin(schedule: np.ndarray, home: int, away: int, matchday: int) -> np.ndarray:
    """A permutation of the rounds that moves the round holding home v away to `matchday` (1-based), by swapping two rounds: apply it as schedule[perm]."""
    perm = np.arange(len(schedule))
    k = int(np.flatnonzero(((schedule[..., 0] == home) & (schedule[..., 1] == away)).any(axis=1))[0])
    perm[[k, matchday - 1]] = perm[[matchday - 1, k]]
    return perm


def standings(home: np.ndarray, away: np.ndarray, home_goals: np.ndarray, away_goals: np.ndarray, n_clubs: int) -> dict[str, np.ndarray]:
    """Per-club totals for fixtures (home[k], away[k]); goals have fixtures on their last axis (any leading axes: seasons, simulations)."""
    hg, ag = np.asarray(home_goals), np.asarray(away_goals)
    on_home = np.zeros((home.size, n_clubs))
    on_home[np.arange(home.size), home] = 1
    on_away = np.zeros((away.size, n_clubs))
    on_away[np.arange(away.size), away] = 1
    hw, dr, aw = (hg > ag).astype(float), (hg == ag).astype(float), (hg < ag).astype(float)
    t = {
        "played": np.broadcast_to(np.ones(home.size) @ (on_home + on_away), hg.shape[:-1] + (n_clubs,)),
        "won": hw @ on_home + aw @ on_away,
        "drawn": dr @ (on_home + on_away),
        "lost": aw @ on_home + hw @ on_away,
        "goals_for": hg @ on_home + ag @ on_away,
        "goals_against": ag @ on_home + hg @ on_away,
    }
    t = {k: np.rint(v).astype(np.int64) for k, v in t.items()}
    t["goal_difference"] = t["goals_for"] - t["goals_against"]
    t["points"] = 3 * t["won"] + t["drawn"]
    return t


def order(points: np.ndarray, goal_difference: np.ndarray, goals_for: np.ndarray) -> np.ndarray:
    """Club indices from first to last: points, then goal difference, then goals scored, then club index (stands in for head-to-head and a play-off)."""
    index = np.broadcast_to(np.arange(points.shape[-1]), points.shape)
    return np.lexsort((index, -goals_for, -goal_difference, -points), axis=-1)


def ranks(points: np.ndarray, goal_difference: np.ndarray, goals_for: np.ndarray) -> np.ndarray:
    """1-based league position of each club."""
    o = order(points, goal_difference, goals_for)
    r = np.empty_like(o)
    np.put_along_axis(r, o, np.broadcast_to(np.arange(1, o.shape[-1] + 1), o.shape), axis=-1)
    return r
