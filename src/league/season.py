"""One seeded season of the Synthetic League: hidden strengths, the results as played, and what a public observer can say about the table.

The truth (attack, defence) drives the results. The observer never sees it: it starts from a public pre-season prior, the truth plus noise, updates it on the goals scored and conceded so far, and plays out the remaining fixtures by Monte Carlo. Every probability in a card comes from the observer, never from the truth.
"""

import dataclasses
from dataclasses import dataclass

import numpy as np

from league.clubs import CLUBS, DEMO_AWAY, DEMO_HOME
from league.model import N_CLUBS, Params, draw_strengths, rates, sample_scores
from league.table import pin, ranks, round_robin, standings

MATCHDAYS = 2 * (N_CLUBS - 1)
PRIOR_NOISE = 0.15  # standard deviation of the public prior around the truth, log scale, on attack and on defence
DEMO_MATCHDAY = 30  # the matchday of Rovers v United, the demo match
N_SIMS = 4000  # Monte Carlo seasons: a probability near 50 % has a standard error under 1 point
TOP, RELEGATED = 4, 3

TABLE_FIELDS = {
    "rank": "league position, 1 to 20, after the matchdays played: points, then goal difference, then goals scored",
    "points_behind_leader": "points of the leader minus the club's points",
    "points_from_top4": "for a top-four club, its points minus those of the 5th club; otherwise its points minus those of the 4th club (negative)",
    "points_clear_of_drop_zone": "for a club ranked 1 to 17, its points minus those of the 18th club; otherwise its points minus those of the 17th club (zero or negative)",
    "title_probability": "chance of finishing 1st, Monte Carlo over the remaining fixtures with the observer's strengths, 0 to 1",
    "top4_probability": "chance of finishing in the top four, same method",
    "relegation_probability": "chance of finishing 18th to 20th, same method",
}


@dataclass(frozen=True, eq=False)
class League:
    seed: int
    params: Params
    clubs: tuple[str, ...]
    attack: np.ndarray  # truth, hidden from the observer
    defence: np.ndarray
    prior_attack: np.ndarray  # public pre-season prior
    prior_defence: np.ndarray
    prior_noise: float
    schedule: np.ndarray  # (38, 10, 2): matchday, match, (home, away) club index
    home_goals: np.ndarray  # (38, 10): the season as played
    away_goals: np.ndarray
    demo_matchday: int

    @classmethod
    def create(cls, seed: int = 1, params: Params = Params(), demo_matchday: int = DEMO_MATCHDAY, prior_noise: float = PRIOR_NOISE) -> "League":
        """The league of a seed. The demo matchday only reorders the calendar: strengths and the result of every fixture stay the same."""
        if not 1 <= demo_matchday <= MATCHDAYS:
            raise ValueError(f"demo_matchday must be between 1 and {MATCHDAYS}")
        r_strength, r_prior, r_calendar, r_results = (np.random.default_rng(s) for s in np.random.SeedSequence(seed).spawn(4))
        attack, defence = draw_strengths(r_strength, params)
        noise = prior_noise * r_prior.standard_normal((2, N_CLUBS))
        schedule = round_robin(N_CLUBS, r_calendar)
        hg, ag = sample_scores(r_results, *rates(params, attack, defence, schedule[..., 0], schedule[..., 1]), params.rho)
        perm = pin(schedule, CLUBS.index(DEMO_HOME), CLUBS.index(DEMO_AWAY), demo_matchday)
        return cls(seed, params, CLUBS, attack, defence, attack + noise[0], defence + noise[1], prior_noise, schedule[perm], hg[perm], ag[perm], demo_matchday)

    def index(self, club: str) -> int:
        return self.clubs.index(club)

    def with_result(self, home: str, away: str, home_goals: int, away_goals: int) -> "League":
        """The same league with the score of one fixture replaced, for instance by the final score of the generated demo match."""
        hit = (self.schedule[..., 0] == self.index(home)) & (self.schedule[..., 1] == self.index(away))
        hg, ag = self.home_goals.copy(), self.away_goals.copy()
        hg[hit], ag[hit] = home_goals, away_goals
        return dataclasses.replace(self, home_goals=hg, away_goals=ag)

    def fixtures(self, first: int, last: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Home, away and goals of matchdays first to last (1-based, inclusive), flattened."""
        s = slice(first - 1, last)
        return self.schedule[s, :, 0].ravel(), self.schedule[s, :, 1].ravel(), self.home_goals[s].ravel(), self.away_goals[s].ravel()

    def table(self, matchday: int) -> dict[str, np.ndarray]:
        """Standings after `matchday` matchdays (0 to 38), per club index, with the rank."""
        _check(matchday)
        home, away, hg, ag = self.fixtures(1, matchday)
        t = standings(home, away, hg, ag, N_CLUBS)
        t["rank"] = ranks(t["points"], t["goal_difference"], t["goals_for"])
        return t

    def observer(self, matchday: int) -> tuple[np.ndarray, np.ndarray]:
        """The observer's attack and defence after `matchday` matchdays.

        Conjugate gamma-Poisson update of each club's scoring and conceding rates, opponents held at their prior: with a gamma prior of shape alpha = 1 / prior_noise^2 (log standard deviation about prior_noise), the rate is multiplied by (alpha + goals) / (alpha + expected goals).
        """
        _check(matchday)
        home, away, hg, ag = self.fixtures(1, matchday)
        lam_h, lam_a = rates(self.params, self.prior_attack, self.prior_defence, home, away)
        observed = standings(home, away, hg, ag, N_CLUBS)
        alpha = 1 / self.prior_noise**2
        exp_for = lam_h @ _onehot(home) + lam_a @ _onehot(away)
        exp_against = lam_a @ _onehot(home) + lam_h @ _onehot(away)
        attack = self.prior_attack + np.log((alpha + observed["goals_for"]) / (alpha + exp_for))
        defence = self.prior_defence + np.log((alpha + observed["goals_against"]) / (alpha + exp_against))
        return attack, defence

    def expected_goals(self, home: str, away: str, matchday: int | None = None) -> tuple[float, float]:
        """Expected goals of a fixture: from the truth with matchday None, from the observer after `matchday` matchdays otherwise."""
        attack, defence = (self.attack, self.defence) if matchday is None else self.observer(matchday)
        lam_h, lam_a = rates(self.params, attack, defence, np.array([self.index(home)]), np.array([self.index(away)]))
        return float(lam_h[0]), float(lam_a[0])

    def rng(self, *key: int) -> np.random.Generator:
        """A generator for the observer's Monte Carlo, fixed by the seed and `key`."""
        return np.random.default_rng([self.seed, 1, *key])

    def simulate_rest(self, matchday: int, n_sims: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """The remaining fixtures after `matchday` matchdays, played `n_sims` times with the observer's strengths: home, away (R,), goals (n_sims, R)."""
        home, away, _, _ = self.fixtures(matchday + 1, MATCHDAYS)
        attack, defence = self.observer(matchday)
        lam_h, lam_a = rates(self.params, attack, defence, home, away)
        hg, ag = sample_scores(rng, np.broadcast_to(lam_h, (n_sims, home.size)), np.broadcast_to(lam_a, (n_sims, home.size)), self.params.rho)
        return home, away, hg, ag

    def final_ranks(self, matchday: int, home: np.ndarray, away: np.ndarray, hg: np.ndarray, ag: np.ndarray) -> np.ndarray:
        """Final positions (n_sims, 20): the table after `matchday` plus simulated remaining fixtures."""
        now = self.table(matchday)
        rest = standings(home, away, hg, ag, N_CLUBS)
        return ranks(*(now[k] + rest[k] for k in ("points", "goal_difference", "goals_for")))


def _check(matchday: int) -> None:
    if not 0 <= matchday <= MATCHDAYS:
        raise ValueError(f"matchday must be between 0 and {MATCHDAYS}")


def _onehot(clubs: np.ndarray) -> np.ndarray:
    m = np.zeros((clubs.size, N_CLUBS))
    m[np.arange(clubs.size), clubs] = 1
    return m


def outcome_probabilities(final: np.ndarray) -> dict[str, np.ndarray]:
    """Per-club chances of the title, the top four and relegation, from final positions (n_sims, 20)."""
    return {
        "title_probability": (final == 1).mean(axis=0),
        "top4_probability": (final <= TOP).mean(axis=0),
        "relegation_probability": (final > N_CLUBS - RELEGATED).mean(axis=0),
    }


def season_state(league: League, matchday: int, n_sims: int = N_SIMS) -> dict:
    """The table after `matchday` matchdays, with points gaps and the observer's chances, in named fields (see `field_meanings`)."""
    t = league.table(matchday)
    probs = outcome_probabilities(league.final_ranks(matchday, *league.simulate_rest(matchday, n_sims, league.rng(matchday))))
    by_rank = np.argsort(t["rank"])

    def pts_at(rank: int) -> int:
        return int(t["points"][by_rank[rank - 1]])

    rows = []
    for i in by_rank:
        rank, pts = int(t["rank"][i]), int(t["points"][i])
        rows.append({
            "rank": rank, "club": league.clubs[i],
            **{k: int(t[k][i]) for k in ("played", "won", "drawn", "lost", "goals_for", "goals_against", "goal_difference", "points")},
            "points_behind_leader": pts_at(1) - pts,
            "points_from_top4": pts - pts_at(TOP + 1) if rank <= TOP else pts - pts_at(TOP),
            "points_clear_of_drop_zone": pts - pts_at(N_CLUBS - RELEGATED + 1) if rank <= N_CLUBS - RELEGATED else pts - pts_at(N_CLUBS - RELEGATED),
            **{k: float(v[i]) for k, v in probs.items()},
        })
    return {"seed": league.seed, "matchday": matchday, "n_sims": n_sims, "table": rows, "field_meanings": TABLE_FIELDS}


def match_importance(league: League, home: str, away: str, matchday: int, n_sims: int = N_SIMS) -> dict[str, dict[str, float]]:
    """What a fixture still to play after `matchday` matchdays means for both clubs' chances, with common random numbers.

    The other remaining fixtures are simulated once; the fixture itself is then set to a 1-0 win for each side in turn. The stake of a club is its chance if it wins minus its chance if it loses: positive for the title and the top four, negative for relegation.
    """
    h, a = league.index(home), league.index(away)
    fh, fa, hg, ag = league.simulate_rest(matchday, n_sims, league.rng(matchday, h, a))
    col = np.flatnonzero((fh == h) & (fa == a))
    if col.size != 1:
        raise ValueError(f"{home} v {away} is not among the fixtures left after matchday {matchday}")
    outcomes = {}
    for winner, (x, y) in (("home", (1, 0)), ("away", (0, 1))):
        hg_, ag_ = hg.copy(), ag.copy()
        hg_[:, col], ag_[:, col] = x, y
        outcomes[winner] = outcome_probabilities(league.final_ranks(matchday, fh, fa, hg_, ag_))
    out = {}
    for club, idx, win, loss in ((home, h, "home", "away"), (away, a, "away", "home")):
        d = {}
        for name in ("title", "top4", "relegation"):
            p_win, p_loss = float(outcomes[win][f"{name}_probability"][idx]), float(outcomes[loss][f"{name}_probability"][idx])
            d |= {f"{name}_if_win": p_win, f"{name}_if_loss": p_loss, f"{name}_stake": p_win - p_loss}
        out[club] = d
    return out
