"""Skellam win-probability model: the goals still to come are Poisson, so the final goal difference is the current one plus a Skellam variable.

Each team scores at a constant rate: its pre-match strength spread evenly over the expected length of a match. The time left includes the added time still to come, as in Opta's live win probability: an expected amount until the fourth official shows the board, the announced amount after. The result is only certain at the final whistle. Red cards scale both rates. The model sees nothing else (shots, momentum, substitutions), which keeps every number it produces easy to explain on air.
"""

import math
from dataclasses import dataclass

from scipy.stats import skellam

from winprob.model import MatchState, WinProb

# Minutes added per half before the board goes up. Premier League matches lasted 99:44 on average in 2024/25 and 100:35 over the first seven matchdays of 2025/26 (Opta), about 10 minutes of added time a match; the split between the halves is an estimate.
EXPECTED_ADDED = (3.5, 6.5)
LAST_PLAY = 0.5  # minutes still to play once the announced added time has run out: the referee ends the half at the next stoppage


@dataclass(frozen=True)
class SkellamWinProb:
    """Win probability from the score, the time left and the red cards.

    Strengths are each team's expected goals for a whole match, before kick-off, added time included, spread evenly over `length` minutes of clock. The defaults are indicative round numbers, not fitted on any data: about 2.7 goals a match, home side slightly stronger. The real values will come from a simulated season.

    The red-card factors multiply the scoring rates once per red card: `red_own` for the team that is a player down, `red_opponent` for its opponent. They are placeholders, to be calibrated.
    """

    home_strength: float = 1.5
    away_strength: float = 1.2
    expected_added: tuple[float, float] = EXPECTED_ADDED
    red_own: float = 0.7
    red_opponent: float = 1.3

    def __post_init__(self) -> None:
        if not all(math.isfinite(v) and v > 0 for v in (self.home_strength, self.away_strength, self.red_own, self.red_opponent)):
            raise ValueError(f"strengths and red-card factors must be finite and positive: {self}")
        if len(self.expected_added) != 2 or not all(math.isfinite(v) and v >= 0 for v in self.expected_added):
            raise ValueError(f"expected_added must be two finite, non-negative numbers of minutes: {self}")

    @property
    def length(self) -> float:
        """Expected length of a match on the clock, added time included."""
        return 90.0 + sum(self.expected_added)

    def minutes_left(self, state: MatchState) -> float:
        if state.final:
            return 0.0
        first, second = self.expected_added
        period = state.period or (1 if state.minute < 45 else 2)
        if period == 1:
            end = 45 + (first if state.added_time is None else state.added_time)
            return max(end - state.minute, LAST_PLAY if state.minute >= 45 else 0.0) + 45 + second
        end = 90 + (second if state.added_time is None else state.added_time)
        return max(end - state.minute, LAST_PLAY)

    def predict(self, state: MatchState) -> WinProb:
        diff = state.home_goals - state.away_goals
        left = self.minutes_left(state) / self.length  # share of a match still to play
        if left == 0:  # final whistle: the result is certain (and scipy's Skellam is undefined with a zero rate)
            return WinProb(float(diff > 0), float(diff == 0), float(diff < 0))
        mu_home = self.home_strength * left * self.red_own**state.home_reds * self.red_opponent**state.away_reds
        mu_away = self.away_strength * left * self.red_own**state.away_reds * self.red_opponent**state.home_reds
        # Final difference = diff + K with K ~ Skellam(mu_home, mu_away): home win if K > -diff, draw if K = -diff.
        home = float(skellam.sf(-diff, mu_home, mu_away))
        draw = float(skellam.pmf(-diff, mu_home, mu_away))
        away = float(skellam.cdf(-diff - 1, mu_home, mu_away))
        total = home + draw + away  # three separate scipy calls: renormalise so they sum to 1
        return WinProb(home / total, draw / total, away / total)
