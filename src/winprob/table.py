"""Fallback win-probability model: a lookup table indexed by minute and goal difference.

It shows that the interface is replaceable: anything with a predict(MatchState) -> WinProb method can stand in for the Skellam model. The table can be filled from another model (from_model) or, later, from the final results of many simulated matches. Probabilities are interpolated linearly between two rows; goal differences beyond the table take its outermost column. Team strengths, red cards and announced added time are ignored; the result is certain at the final whistle.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from winprob.model import MatchState, WinProb, WinProbModel


@dataclass(frozen=True)
class TableWinProb:
    minutes: tuple[float, ...]  # one row per minute, increasing
    rows: tuple[tuple[WinProb, ...], ...]  # rows[i][d + max_diff]: goal difference d (home minus away) at minutes[i]

    def __post_init__(self) -> None:
        widths = {len(row) for row in self.rows}
        if len(self.rows) != len(self.minutes) or list(self.minutes) != sorted(set(self.minutes)) or len(widths) != 1 or widths.pop() % 2 == 0:
            raise ValueError("expected increasing minutes, one row per minute and rows of the same odd length")

    @property
    def max_diff(self) -> int:
        return len(self.rows[0]) // 2

    @classmethod
    def from_model(cls, model: WinProbModel, minutes: Sequence[float] | None = None, max_diff: int = 4) -> "TableWinProb":
        """Tabulates another model on a grid of minutes and goal differences, with no red cards. The default grid has a row every 5 minutes up to the model's expected match length (its `length`, 100 if it has none)."""
        if minutes is None:
            end = float(getattr(model, "length", 100.0))
            minutes = (*range(0, math.ceil(end), 5), end)
        rows = tuple(tuple(model.predict(MatchState(float(m), max(d, 0), max(-d, 0))) for d in range(-max_diff, max_diff + 1)) for m in minutes)
        return cls(tuple(float(m) for m in minutes), rows)

    def predict(self, state: MatchState) -> WinProb:
        if state.final:
            d = state.home_goals - state.away_goals
            return WinProb(float(d > 0), float(d == 0), float(d < 0))
        diff = min(max(state.home_goals - state.away_goals, -self.max_diff), self.max_diff)
        cells = [row[diff + self.max_diff] for row in self.rows]
        home = float(np.interp(state.minute, self.minutes, [c.home for c in cells]))
        draw = float(np.interp(state.minute, self.minutes, [c.draw for c in cells]))
        away = float(np.interp(state.minute, self.minutes, [c.away for c in cells]))
        return WinProb(home, draw, away)
