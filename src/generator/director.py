"""The script: planted moments that the engine reaches through normal play.

A moment is a shot or a foul at a set second of the match clock, or a substitution at half-time. As a shot or a foul nears, background play calms down (no fouls, shots or balls out of play), then the Director takes over at the right time: the scripted team wins the ball back if needed, passes it forward and plays the final ball, so that the shot or the foul happens exactly on time. Every planted action still goes through the ActionModel and the Laws like any other.

The demo story is the one shown in the video. Any other seed has no script: goals come from shots and their xG, and VAR checks from offsides the assistant missed.
"""

import math
from dataclasses import dataclass

from generator.actions import Action
from generator.pitch import LENGTH, MID_Y, inside, to_frame

LEAD_S = 150  # background play stays calm this long before a moment


@dataclass
class Moment:
    key: str
    kind: str  # goal, save, foul or substitution
    side: str  # the attacking team (the shooter's, or the fouled player's), or the team that makes the substitution
    shirt: int  # the shooter, the fouled player, or the player who comes on
    at_s: float  # match clock of the shot or the foul; 45:00 for a half-time substitution
    spot: tuple[float, float] = (0.0, 0.0)  # where it happens, in the attacking team's frame
    bodypart: str = "foot_right"
    assist: int | None = None
    offside_margin: float | None = None  # the assist releases a runner this far beyond the line, and the assistant misses it
    var: dict | None = None  # announced VAR check: after_s, duration_s, decision, reason
    correction: dict | None = None  # provider correction after the goal: after_s, to (shirt), cause, note
    offender: int | None = None  # foul: the defender who commits it
    card: str | None = None  # foul: yellow, second_yellow or red
    reason: str | None = None
    keeper: str | None = None  # save: "parry_corner"
    deflector: int | None = None  # team-mate standing in the path of the shot
    out: int | None = None  # substitution: the player who goes off

    @property
    def period(self) -> int:
        return 1 if self.at_s < 45 * 60 else 2

    def players(self) -> set[str]:
        other = "away" if self.side == "home" else "home"
        ids = {f"{self.side}-{n}" for n in (self.shirt, self.assist, self.deflector, self.out) if n}
        if self.offender:
            ids.add(f"{other}-{self.offender}")
        if self.kind == "save":
            ids.add(f"{other}-1")
        return ids


def _mmss(minutes: int, seconds: int) -> float:
    return minutes * 60.0 + seconds


# Rovers 0-2 United at half-time, then Rovers' substitute #20 comes on and the match turns: 5-2. The 63' goal is the central moment of the video: its card goes on air during the VAR check, then the goal is ruled out for offside and the card is retracted. United's 69' equaliser is ruled out too, for handball.
DEMO_SCRIPT = [
    Moment("goal-14", "goal", "away", 9, _mmss(13, 20), (93.0, 36.0), assist=10),
    Moment("goal-31", "goal", "away", 10, _mmss(30, 40), (92.0, 31.0), assist=11, deflector=7,
           correction={"after_s": 110, "to": 7, "cause": "deflection", "note": "Shot deflected in by #7: goal credited to him"}),
    Moment("yellow-41", "foul", "home", 10, _mmss(40, 15), (70.0, 40.0), assist=8, offender=5, card="yellow", reason="stopping a promising attack"),
    Moment("sub-46", "substitution", "home", 20, _mmss(45, 0), out=9),
    Moment("goal-52", "goal", "home", 20, _mmss(51, 10), (95.0, 33.0), assist=10),
    Moment("goal-57", "goal", "home", 20, _mmss(56, 25), (92.5, 38.5), "foot_left", assist=11),
    Moment("goal-63", "goal", "home", 20, _mmss(62, 25), (97.0, 31.5), assist=10, offside_margin=0.25,
           var={"after_s": 15, "duration_s": 70, "decision": "overturned", "reason": "possible offside"}),
    Moment("goal-66", "goal", "home", 20, _mmss(65, 50), (94.0, 30.0), assist=7),
    Moment("goal-69", "goal", "away", 9, _mmss(68, 20), (94.0, 35.0), assist=11,
           var={"after_s": 15, "duration_s": 60, "decision": "overturned", "reason": "possible handball", "offence": "handball"}),
    Moment("save-72", "save", "away", 11, _mmss(71, 35), (95.0, 36.0), assist=10, keeper="parry_corner"),
    Moment("red-78", "foul", "home", 20, _mmss(77, 30), (80.0, 30.0), assist=8, offender=5, card="second_yellow", reason="a second bookable offence"),
    Moment("goal-83", "goal", "home", 20, _mmss(82, 40), (96.0, 35.5), assist=8),
    Moment("goal-88", "goal", "home", 10, _mmss(87, 20), (91.5, 29.0), assist=20,
           var={"after_s": 15, "duration_s": 40, "decision": "stands", "reason": "possible handball"}),
]


class Director:
    def __init__(self, engine, moments: list[Moment]) -> None:
        self.e = engine
        self.rng = engine.rng
        self.pending = sorted(moments, key=lambda m: m.at_s)
        self.plan: list[Action] = []
        self.scripted = bool(moments)
        # Without a script, background play may score, give penalties and send players off. With one, only planted moments may.
        self.background_goals = self.background_reds = not self.scripted

    def calm(self) -> bool:
        if self.plan:
            return True
        s = self.e.state
        m = self.pending[0] if self.pending else None
        return m is not None and m.kind != "substitution" and m.period == s.clock.period and m.at_s - s.clock.clock_s < LEAD_S

    def protected(self, player_id: str) -> bool:
        """Players a coming moment needs: never substituted before it, and never booked by background play."""
        return any(player_id in m.players() for m in self.pending)

    def shot_cap(self) -> float | None:
        """Background shots stay below the planted chances: small xG, smaller still late on, when a goal would swing the result most."""
        if not self.scripted:
            return None
        return 0.12 if self.e.state.clock.clock_s < 75 * 60 else 0.06

    def half_time(self) -> None:
        """Planted substitutions made during the break, before the second-half kick-off."""
        s = self.e.state
        while self.pending and self.pending[0].kind == "substitution":
            m = self.pending.pop(0)
            team = s.teams[m.side]
            out, incoming = team.player(m.out), team.player(m.shirt)
            s.lineups[m.side].substitute(out, incoming)
            msg = self.e.message("substitution", team=m.side, player_out=out.ref(), player_in=incoming.ref())
            self.e.truth.moment(m, msg)

    def next(self) -> Action | None:
        if self.plan:
            return self.plan.pop(0)
        s = self.e.state
        if not self.pending or not s.ball.alive:
            return None
        m = self.pending[0]
        if m.kind == "substitution" or m.period != s.clock.period:
            return None
        bx, by = to_frame(m.side, s.ball.x, s.ball.y)
        steps = max(1, math.ceil(math.hypot(m.spot[0] - 17 - bx, m.spot[1] - by) / 17))
        need = (1.0 if s.possession != m.side else 0.0) + steps * 2.5 + 2.4
        if s.clock.clock_s + need < m.at_s - 0.5:
            return None
        self.pending.pop(0)
        self.plan = self._plan(m)
        # stretch or squeeze the planted actions so that the last one starts on time
        timed = self.plan[:-1]
        total = sum(a.duration for a in timed)
        left = m.at_s - s.clock.clock_s
        if total > 0 and left > 0:
            scale = min(max(left / total, 0.5), 2.0)
            for a in timed:
                a.duration *= scale
        return self.plan.pop(0)

    def _plan(self, m: Moment) -> list[Action]:
        e, s, rng = self.e, self.e.state, self.rng
        side = m.side
        other = s.other(side)
        team, lineup = s.teams[side], s.lineups[side]
        pos = e.positions()
        plan: list[Action] = []
        holder = s.ball.owner
        if s.possession != side:
            holder = e.actions.nearest_player(side, (s.ball.x, s.ball.y), outfield=True)
            plan.append(Action("recover", holder, duration=1.0))
        target_player = team.player(m.shirt)
        assist = team.player(m.assist) if m.assist else None
        bx, by = to_frame(side, s.ball.x, s.ball.y)
        sx, sy = m.spot
        ax, ay = inside(sx - 17.0, sy + float(rng.normal(0, 5)), 6.0)
        steps = max(1, math.ceil(math.hypot(ax - bx, ay - by) / 17))
        taken = {target_player.id, assist.id if assist else ""}
        x, y = bx, by
        for k in range(1, steps + 1):
            if k == steps:
                px, py, target = ax, ay, assist
            else:
                px = bx + (ax - bx) * k / steps + float(rng.normal(0, 2))
                py = by + (ay - by) * k / steps + float(rng.normal(0, 4))
                px, py = inside(px, py, 3.0)
                options = [p for p in lineup.outfield() if p.id not in taken and p.id != holder.id]
                target = min(options, key=lambda p: math.dist(to_frame(side, *pos[p.id]), (px, py)))
            length = math.hypot(px - x, py - y)
            if target is None or target.id == holder.id:
                plan.append(Action("dribble", holder, end=(px, py), result="success", duration=length / 5.5 + 0.5))
            else:
                plan.append(Action("pass", holder, target=target, end=(px, py), result="success", duration=length / 15 + 1.3))
                holder = target
            x, y = px, py
        final = math.hypot(sx - x, sy - y)
        if m.kind == "foul":
            offender = s.teams[other].player(m.offender)
            if m.card == "red":  # a clear run on goal, stopped by a foul
                plan.append(Action("through_ball", holder, target=target_player, end=(sx, sy), result="success", duration=final / 16 + 1.1, extra={"margin": -1.5}))
            else:
                plan.append(Action("pass", holder, target=target_player, end=(sx, sy), result="success", duration=final / 15 + 1.1))
            plan.append(Action("foul", offender, target=target_player, extra={"card": m.card, "reason": m.reason, "moment": m, "pins": {offender.id: (sx - 1.2, sy + 0.8)}}))
            return plan
        if m.offside_margin is not None:
            plan.append(Action("through_ball", holder, target=target_player, end=(sx, sy), result="success", duration=final / 16 + 1.1, extra={"margin": m.offside_margin, "flagged": False}))
        else:
            plan.append(Action("pass", holder, target=target_player, end=(sx, sy), result="success", duration=final / 15 + 1.0))
        pins = {}
        if m.deflector:
            t = 0.55
            pins[f"{side}-{m.deflector}"] = (sx + t * (LENGTH - sx), sy + t * (MID_Y - sy) + 0.3)
        outcome = "goal" if m.kind == "goal" else "saved"
        plan.append(Action("shot", target_player, result=outcome, extra={"moment": m, "bodypart": m.bodypart, "var": m.var, "keeper": m.keeper, "pins": pins}))
        return plan
