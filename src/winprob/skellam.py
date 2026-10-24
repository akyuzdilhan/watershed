"""Skellam win-probability model: the goals still to come are Poisson, so the final goal difference is the current one plus a Skellam variable.

Each team scores at a constant rate: its pre-match strength spread evenly over the minutes of play, stoppage time included. Red cards scale both rates. The model sees nothing else (shots, momentum, substitutions), which keeps every number it produces easy to explain on air.
"""

import math
from dataclasses import dataclass

from scipy.stats import skellam

from winprob.model import MatchState, WinProb


@dataclass(frozen=True)
class SkellamWinProb:
    """Win probability from the score, the time left and the red cards.

    Strengths are each team's expected goals for a whole match, before kick-off: a per-match figure, stoppage time included, spread evenly over `duration`. The defaults are indicative round numbers, not fitted on any data: about 2.7 goals a match, home side slightly stronger. The real values will come from a simulated season.

    `duration` is the length of a match on the clock, stoppage time included, and a state at or past it is full time, with a certain result. If a match runs longer, the caller keeps the minute just below `duration` until the final whistle.

    The red-card factors multiply the scoring rates once per red card: `red_own` for the team that is a player down, `red_opponent` for its opponent. They are placeholders, to be calibrated.
    """

    home_strength: float = 1.5
    away_strength: float = 1.2
    duration: float = 95.0
    red_own: float = 0.7
    red_opponent: float = 1.3

    def __post_init__(self) -> None:
        if not all(math.isfinite(v) and v > 0 for v in (self.home_strength, self.away_strength, self.duration, self.red_own, self.red_opponent)):
            raise ValueError(f"every parameter must be finite and positive: {self}")

    def predict(self, state: MatchState) -> WinProb:
        diff = state.home_goals - state.away_goals
        left = max(self.duration - state.minute, 0.0) / self.duration  # share of the match still to play
        if left == 0:  # full time: the result is certain (and scipy's Skellam is undefined with a zero rate)
            return WinProb(float(diff > 0), float(diff == 0), float(diff < 0))
        mu_home = self.home_strength * left * self.red_own**state.home_reds * self.red_opponent**state.away_reds
        mu_away = self.away_strength * left * self.red_own**state.away_reds * self.red_opponent**state.home_reds
        # Final difference = diff + K with K ~ Skellam(mu_home, mu_away): home win if K > -diff, draw if K = -diff.
        home = float(skellam.sf(-diff, mu_home, mu_away))
        draw = float(skellam.pmf(-diff, mu_home, mu_away))
        away = float(skellam.cdf(-diff - 1, mu_home, mu_away))
        total = home + draw + away  # three separate scipy calls: renormalise so they sum to 1
        return WinProb(home / total, draw / total, away / total)
