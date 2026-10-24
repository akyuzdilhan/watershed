"""Win-probability interface: the match state, the three outcome probabilities and the model protocol.

The worker only depends on WinProbModel, so the model behind it can be swapped: the Skellam model (winprob.skellam), the lookup table (winprob.table), or a model trained on real match data by whoever runs the system.
"""

import math
from dataclasses import dataclass, replace
from typing import Literal, Protocol

Team = Literal["home", "away"]


@dataclass(frozen=True)
class MatchState:
    minute: float  # match clock in minutes, stoppage time included, as in the feed (clock_s / 60: the second half restarts at 45)
    home_goals: int
    away_goals: int
    home_reds: int = 0
    away_reds: int = 0

    def with_goal(self, team: Team) -> "MatchState":
        """The same state with one more goal for `team`: the counterfactual "if the shot had gone in"."""
        if team == "home":
            return replace(self, home_goals=self.home_goals + 1)
        if team == "away":
            return replace(self, away_goals=self.away_goals + 1)
        raise ValueError(f"team must be 'home' or 'away', not {team!r}")


@dataclass(frozen=True)
class WinProb:
    """Probabilities of a home win, a draw and an away win at the final whistle."""

    home: float
    draw: float
    away: float

    def __post_init__(self) -> None:
        if min(self.home, self.draw, self.away) < 0 or not math.isclose(self.home + self.draw + self.away, 1.0, abs_tol=1e-9):
            raise ValueError(f"not a probability distribution: {self}")


class WinProbModel(Protocol):
    def predict(self, state: MatchState) -> WinProb:
        """Probabilities of the final result, given the state of the match."""
        ...
