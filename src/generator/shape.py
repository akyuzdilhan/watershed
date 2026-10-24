"""Team shape: where the 22 players stand when an event happens.

Each team keeps its formation and slides with the ball: a longer, wider shape in possession, a compact block out of possession. Attackers stay just onside unless a run is planted, the nearest defender presses the ball carrier, and every player carries a small persistent offset so that two frames never look alike. This is not tracking data: it gives each event a 360-style freeze frame, the offside line and the pressure on the shooter.
"""

import math

import numpy as np

from generator.pitch import HALF_X, MID_Y, from_frame, in_cone, inside, to_frame
from generator.state import MatchState
from generator.teams import FORMATIONS

SLIDE_X = 0.5  # how far the shape follows the ball along the pitch
SLIDE_Y = {True: 0.25, False: 0.35}  # towards the ball side, in and out of possession
WIDTH = {True: 1.0, False: 0.8}
DEEPEST = 9.0  # metres from its own goal line


class Shape:
    def __init__(self, rng: np.random.Generator, state: MatchState) -> None:
        self.rng, self.state = rng, state
        self.offset: dict[str, tuple[float, float]] = {}
        self.pins: dict[str, list] = {}  # player id -> [engine-frame position, engine steps left]: runs that the formation alone would not produce
        self.version = 0  # changes with the pins, so cached positions are recomputed

    def pin(self, player_id: str, xy: tuple[float, float], steps: int = 1) -> None:
        """Forces a player's position for the current engine step and the `steps - 1` following ones."""
        self.pins[player_id] = [xy, steps]
        self.version += 1

    def tick(self) -> None:
        """Called at each engine step: pins expire."""
        for pid in list(self.pins):
            self.pins[pid][1] -= 1
            if self.pins[pid][1] <= 0:
                del self.pins[pid]
                self.version += 1

    def positions(self) -> dict[str, tuple[float, float]]:
        """Engine-frame position of every player on the pitch."""
        s = self.state
        attacking = s.possession
        defending = s.other(attacking)
        pos: dict[str, tuple[float, float]] = {}
        noise = self.rng.normal(0, 0.6, (22, 2)).tolist()  # each player's offset drifts as an AR(1), about 1.4 m standard deviation
        k = 0
        for side in (defending, attacking):
            team, lineup = s.teams[side], s.lineups[side]
            bx, by = to_frame(side, s.ball.x, s.ball.y)
            has_ball = side == attacking
            for i, p in enumerate(lineup.slots):
                if p is None:
                    continue
                role, ipx, ipy, oopx, oopy = FORMATIONS[team.formation][i]
                if role == "GK":
                    x = min(max(6 + 0.12 * bx, 4), 20) if has_ball else min(max(2.5 + 0.06 * bx, 1.5), 9)
                    y = MID_Y + (0.1 if has_ball else 0.2) * (by - MID_Y)
                else:
                    old = self.offset.get(p.id, (0.0, 0.0))
                    dx, dy = 0.9 * old[0] + noise[k][0], 0.9 * old[1] + noise[k][1]
                    self.offset[p.id] = (dx, dy)
                    k += 1
                    base_x, base_y = (ipx, ipy) if has_ball else (oopx + team.line_height, oopy)
                    x = base_x + SLIDE_X * (bx - HALF_X) + dx
                    if not has_ball:
                        x = max(x, min(DEEPEST, bx - 1.0))  # the block does not drop deeper than the penalty spot area unless the ball is there
                    y = MID_Y + (base_y - MID_Y) * WIDTH[has_ball] + SLIDE_Y[has_ball] * (by - MID_Y) + dy
                pos[p.id] = from_frame(side, *inside(x, y))

        self._stay_onside(pos, attacking, defending)
        self._press(pos, attacking, defending)
        if s.ball.owner is not None and s.ball.owner.id in pos:
            pos[s.ball.owner.id] = (s.ball.x, s.ball.y)
        pos.update({pid: pin[0] for pid, pin in self.pins.items() if pid in pos and (s.ball.owner is None or pid != s.ball.owner.id)})
        return pos

    def _stay_onside(self, pos: dict, attacking: str, defending: str) -> None:
        line = max(self.line(pos, defending), to_frame(attacking, self.state.ball.x, self.state.ball.y)[0])
        for p in self.state.lineups[attacking].outfield():
            x, y = to_frame(attacking, *pos[p.id])
            if x > HALF_X and x > line - 0.3:
                pos[p.id] = from_frame(attacking, max(line - float(self.rng.uniform(0.3, 2.5)), HALF_X), y)

    def _press(self, pos: dict, attacking: str, defending: str) -> None:
        """The defender nearest to the ball closes it down to between 1.2 and 5 m."""
        ball = (self.state.ball.x, self.state.ball.y)
        presser, dist = self.nearest(pos, defending, ball)
        if presser is not None and dist < 15:
            gap = float(self.rng.uniform(1.2, 5.0))
            if dist > gap:
                px, py = pos[presser]
                t = gap / dist
                pos[presser] = (ball[0] + (px - ball[0]) * t, ball[1] + (py - ball[1]) * t)

    def nearest(self, pos: dict, side: str, point: tuple[float, float], exclude: str | None = None) -> tuple[str | None, float]:
        best, best_d = None, math.inf
        for p in self.state.lineups[side].on_pitch():
            if p.id == exclude:
                continue
            d = math.dist(pos[p.id], point)
            if d < best_d:
                best, best_d = p.id, d
        return best, best_d

    def line(self, pos: dict, defending: str) -> float:
        """Offside line in the attacking team's frame: the second-last defender, goalkeeper included."""
        attacking = self.state.other(defending)
        xs = sorted((to_frame(attacking, *pos[p.id])[0] for p in self.state.lineups[defending].on_pitch()), reverse=True)
        return xs[1] if len(xs) > 1 else HALF_X

    def blockers(self, pos: dict, shooter_side: str, shot_at: tuple[float, float]) -> int:
        """Outfield defenders standing in the shooting cone, from the shooter's frame."""
        defending = self.state.other(shooter_side)
        return sum(
            in_cone(shot_at, to_frame(shooter_side, *pos[p.id])) for p in self.state.lineups[defending].outfield()
        )

    def frame(self, pos: dict, actor_side: str, actor_id: str | None) -> list[dict]:
        """360-style freeze frame in the acting team's frame (StatsBomb open data uses the same four keys, without player ids)."""
        frame = []
        for side in (actor_side, self.state.other(actor_side)):
            for p in self.state.lineups[side].on_pitch():
                x, y = to_frame(actor_side, *pos[p.id])
                frame.append(
                    {"player_id": p.id, "teammate": side == actor_side, "actor": p.id == actor_id, "keeper": self.state.lineups[side].is_keeper(p), "location": [round(x, 1), round(y, 1)]}
                )
        return frame
