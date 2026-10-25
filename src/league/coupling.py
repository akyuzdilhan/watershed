"""Season to match: what the league gives the demo fixture, Varleigh v Yarnford.

Two sets of expected goals, kept apart so the win-probability model never reads the generator's hidden truth: `expected_goals` comes from the true strengths and is meant for the match generator, `prior_expected_goals` from the observer and is meant for the win-probability model (match_info). The context is for the cards: positions, points, chances and stakes, in named fields with integer percentages.
"""

from league.clubs import DEMO_AWAY, DEMO_HOME
from league.model import Params
from league.season import DEMO_MATCHDAY, N_SIMS, League, match_importance, season_state

CONTEXT_FIELDS = {
    "matchday": "matchday of the fixture; the table is the one after the previous matchday",
    "{side}_rank": "league position before kick-off",
    "{side}_points": "points before kick-off",
    "{side}_played": "matches played before kick-off",
    "points_gap_home_minus_away": "home points minus away points before kick-off",
    "{side}_points_behind_leader": "points of the leader minus the club's points",
    "{side}_points_from_top4": "see season_state: margin over the 5th club, or gap to the 4th club (negative)",
    "{side}_points_clear_of_drop_zone": "see season_state: margin over the 18th club, or gap to the 17th club (zero or negative)",
    "{side}_{event}_chance_pct": "observer's chance of the event (title, top4, relegation) before kick-off, in %",
    "{side}_{event}_chance_if_win_pct": "the same chance if the club wins this match, in %",
    "{side}_{event}_chance_if_loss_pct": "the same chance if the club loses this match, in %",
    "{side}_{event}_stake_pct_points": "chance if it wins minus chance if it loses, in percentage points (computed before rounding)",
}


def _pct(p: float) -> int:
    return int(round(100 * p))


def demo_fixture(seed: int = 1, matchday: int = DEMO_MATCHDAY, params: Params = Params(), n_sims: int = N_SIMS, league: League | None = None) -> dict:
    """Expected goals and card context for Varleigh (home) v Yarnford at `matchday` of the league of `seed`."""
    if league is None:
        league = League.create(seed, params, demo_matchday=matchday)
    elif league.demo_matchday != matchday:
        raise ValueError(f"this league plays the demo fixture on matchday {league.demo_matchday}, not {matchday}")
    before = matchday - 1
    state = {row["club"]: row for row in season_state(league, before, n_sims)["table"]}
    stakes = match_importance(league, DEMO_HOME, DEMO_AWAY, before, n_sims)
    context: dict = {"competition": "Synthetic League", "matchday": matchday, "home_club": DEMO_HOME, "away_club": DEMO_AWAY}
    for side, club in (("home", DEMO_HOME), ("away", DEMO_AWAY)):
        row, st = state[club], stakes[club]
        context |= {f"{side}_{k}": row[k] for k in ("rank", "points", "played", "points_behind_leader", "points_from_top4", "points_clear_of_drop_zone")}
        for event in ("title", "top4", "relegation"):
            context |= {
                f"{side}_{event}_chance_pct": _pct(row[f"{event}_probability"]),
                f"{side}_{event}_chance_if_win_pct": _pct(st[f"{event}_if_win"]),
                f"{side}_{event}_chance_if_loss_pct": _pct(st[f"{event}_if_loss"]),
                f"{side}_{event}_stake_pct_points": _pct(st[f"{event}_stake"]),
            }
    context["points_gap_home_minus_away"] = context["home_points"] - context["away_points"]
    context["field_meanings"] = CONTEXT_FIELDS
    truth = league.expected_goals(DEMO_HOME, DEMO_AWAY)
    prior = league.expected_goals(DEMO_HOME, DEMO_AWAY, before)
    return {
        "seed": league.seed,
        "matchday": matchday,
        "expected_goals": {"home": round(truth[0], 2), "away": round(truth[1], 2)},
        "prior_expected_goals": {"home": round(prior[0], 2), "away": round(prior[1], 2)},
        "context": context,
    }
