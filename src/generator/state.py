"""The ball and the state of the match. Locations are in the engine frame: the home team attacks towards x = 105."""

from dataclasses import dataclass, field

from generator.clock import Clock
from generator.pitch import CENTRE
from generator.teams import Lineup, Player, Team


@dataclass
class Restart:
    """A restart the referee has ordered: who takes it, where, and whether a direct shot is allowed."""

    kind: str  # kick_off, throw_in, goal_kick, corner, free_kick, penalty
    side: str
    x: float
    y: float
    direct: bool = True
    delay_s: float = 0.0  # dead time before the restart
    lost_s: float = 0.0  # part of that time the referee adds back at the end of the half


@dataclass
class Ball:
    x: float = CENTRE[0]
    y: float = CENTRE[1]
    owner: Player | None = None
    alive: bool = False
    restart: Restart | None = None


@dataclass
class MatchState:
    teams: dict[str, Team]
    lineups: dict[str, Lineup]
    clock: Clock = field(default_factory=Clock)
    ball: Ball = field(default_factory=Ball)
    score: dict[str, int] = field(default_factory=lambda: {"home": 0, "away": 0})
    phase: str = "pre_match"  # in_play, stopped, var_check, half_time, full_time
    possession: str = "home"  # team in control of the ball
    possession_id: int = 0
    yellows: dict[str, int] = field(default_factory=dict)  # player id -> yellow cards
    reds: dict[str, int] = field(default_factory=lambda: {"home": 0, "away": 0})
    missed_offside: dict | None = None  # a marginal offside the assistant missed, in the current attack: VAR checks it if a goal follows

    def other(self, side: str) -> str:
        return "away" if side == "home" else "home"

    def give(self, side: str, owner: Player | None) -> None:
        """Hands the ball to `side`; a change of team starts a new possession."""
        if side != self.possession:
            self.possession_id += 1
            self.missed_offside = None
        self.possession, self.ball.owner = side, owner
