"""Match clock and broadcast clock.

The match clock follows the Opta and StatsBomb convention: it starts at 0 and the second half restarts at 45:00, so a clock past 45:00 in the first half is added time ("45+2'"). The broadcast clock (`wall_s`) runs from the first kick-off without ever stopping, half-time included, so every message of a match can be ordered on it.
"""

from dataclasses import dataclass, field

HALF_S = 45 * 60.0
HALFTIME_S = 15 * 60.0  # half-time break on the broadcast clock
MIN_ADDED = {1: 1, 2: 3}  # minutes announced at least, per half


def minute_label(period: int, clock_s: float) -> str:
    """Broadcast label of a match clock: "67'", "45+2'", "90+4'"."""
    minute, end = int(clock_s // 60), 45 * period
    return f"{end}+{minute - end + 1}'" if minute >= end else f"{minute + 1}'"


@dataclass
class Clock:
    period: int = 1
    clock_s: float = 0.0
    wall_s: float = 0.0
    stoppage_s: dict[int, float] = field(default_factory=lambda: {1: 0.0, 2: 0.0})  # time lost to goals, substitutions, cards, injuries and VAR checks
    added_min: dict[int, int | None] = field(default_factory=lambda: {1: None, 2: None})  # announced by the fourth official
    over_s: dict[int, float] = field(default_factory=lambda: {1: 0.0, 2: 0.0})  # played beyond the announced time

    def start(self, period: int) -> None:
        if period == 2:
            self.wall_s += HALFTIME_S
        self.period, self.clock_s = period, HALF_S * (period - 1)

    def advance(self, seconds: float, stoppage: bool = False) -> None:
        self.clock_s += seconds
        self.wall_s += seconds
        if stoppage:
            self.stoppage_s[self.period] += seconds

    @property
    def minute(self) -> int:
        return int(self.clock_s // 60)

    @property
    def label(self) -> str:
        return minute_label(self.period, self.clock_s)

    @property
    def regulation_end(self) -> float:
        return HALF_S * self.period

    def announce(self) -> int:
        """Minimum added time, in whole minutes, from the time lost so far in this half."""
        minutes = max(MIN_ADDED[self.period], round(self.stoppage_s[self.period] / 60))
        self.added_min[self.period] = minutes
        self.stoppage_s[self.period] = 0.0  # what is lost from now on extends the half beyond the announcement
        return minutes

    @property
    def end_s(self) -> float | None:
        """When the referee ends the half, once the added time is announced."""
        added = self.added_min[self.period]
        if added is None:
            return None
        return self.regulation_end + 60 * added + self.stoppage_s[self.period] + self.over_s[self.period]

    def fields(self) -> dict:
        """The clock fields of a feed message, all derived from the rounded clock so that they always agree."""
        clock_s = round(self.clock_s, 1)
        return {"period": self.period, "clock_s": clock_s, "minute": int(clock_s // 60), "minute_label": minute_label(self.period, clock_s)}
