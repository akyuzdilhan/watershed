"""What the player on the ball does, and what comes of it.

ActionModel.choose picks the next action from the place on the pitch, the pressure and the balance of the match. ActionModel.resolve plays it out: it emits the events, moves the ball and hands it on. The rules (out of play, fouls, cards, offside, goals) are left to Laws. Probabilities are hand-tuned on Premier League orders of magnitude (generator.calibration).
"""

import math
from dataclasses import dataclass, field

from generator.pitch import (
    HALF_X, LENGTH, MID_Y, PENALTY_XG, POSTS, WIDTH, from_frame, in_box, inside, to_frame, xg,
)
from generator.teams import GROUP, SHOOTING, Player


@dataclass
class Action:
    kind: str  # pass, through_ball, cross, dribble, take_on, shot, clearance, recover, foul
    player: Player
    target: Player | None = None
    end: tuple[float, float] | None = None  # in the acting team's frame
    result: str | None = None  # outcome forced by the Director: "success", or a shot outcome
    duration: float | None = None  # seconds until the next action
    extra: dict = field(default_factory=dict)


def _sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-z))


class ActionModel:
    def __init__(self, engine) -> None:
        self.e = engine
        self.rng = engine.rng

    @property
    def s(self):
        return self.e.state

    def edge(self, side: str) -> float:
        """Log-scale advantage of `side` right now: pre-match strengths, red cards, and the score late on."""
        s = self.s
        other = s.other(side)
        q = math.log(s.teams[side].expected_goals / s.teams[other].expected_goals)
        q += 0.3 * (s.reds[other] - s.reds[side])
        lead = s.score[side] - s.score[other]
        late = min(max((s.clock.clock_s - 3600) / 1800, 0.0), 1.0)
        if lead < 0:
            q += 0.3 * late
        elif lead > 0:
            q -= 0.2 * late
        return q

    def here(self, side: str) -> tuple[float, float]:
        return to_frame(side, self.s.ball.x, self.s.ball.y)

    def foot(self, p: Player) -> str:
        strong = "foot_left" if p.foot == "left" else "foot_right"
        weak = "foot_right" if p.foot == "left" else "foot_left"
        return strong if self.rng.random() < 0.88 else weak

    def wait(self, seconds: float) -> None:
        self.s.clock.advance(seconds)

    def nearest_player(self, side: str, point: tuple[float, float], exclude: str | None = None, outfield: bool = False) -> Player:
        pos = self.e.positions()
        best, best_d = None, math.inf
        for p in self.s.lineups[side].on_pitch():
            if p.id == exclude or (outfield and self.s.lineups[side].is_keeper(p)):
                continue
            d = math.dist(pos[p.id], point)
            if d < best_d:
                best, best_d = p, d
        return best

    def foul_odds(self, victim: Player, at: tuple[float, float]) -> float:
        """How likely a challenge here is a foul, relative to midfield: none while a planted moment is being set up, fewer in the box (defenders avoid giving penalties), and none there in a story where only planted shots may score."""
        if self.e.director.calm():
            return 0.0
        if in_box(*to_frame(victim.side, *at)):
            return 0.08 if self.e.director.background_goals else 0.0
        return 1.0

    # --- choice ---------------------------------------------------------------------------------

    def choose(self) -> Action:
        s, rng = self.s, self.rng
        p = s.ball.owner
        side = p.side
        x, y = self.here(side)
        pos = self.e.positions()
        calm = self.e.director.calm()
        if s.lineups[side].is_keeper(p):
            return Action("pass", p, extra={"mode": "long" if rng.random() < 0.35 else "short"})
        q = self.edge(side)
        press = self.e.shape.nearest(pos, s.other(side), (s.ball.x, s.ball.y))[1]
        if not calm and x > 72:
            chance = xg(x, y, blockers=self.e.shape.blockers(pos, side, (x, y)))
            cap = self.e.director.shot_cap()
            if chance > 0.012 and (cap is None or chance <= cap):
                p_shot = min(0.45, (0.010 + 0.295 * math.sqrt(chance)) * math.sqrt(SHOOTING[p.role]) * math.exp(0.6 * q) * (1.0 if in_box(x, y) else 0.5))
                if rng.random() < p_shot:
                    return Action("shot", p)
        r = rng.random()
        if not calm:
            if x > 75 and (y < 17 or y > WIDTH - 17) and r < 0.3:
                return Action("cross", p)
            if 40 < x < 86 and r < 0.04:
                return Action("through_ball", p)
        if x < 28 and press < 2.5 and r < 0.3:
            return Action("clearance", p)
        r = rng.random()
        if not calm and r < 0.015 + 0.03 * (x > 60):
            return Action("take_on", p)
        if r < 0.33:
            return Action("dribble", p)
        return Action("pass", p)

    def resolve(self, a: Action) -> None:
        getattr(self, f"_{a.kind}")(a)

    # --- passes ---------------------------------------------------------------------------------

    def _receiver(self, p: Player, pos: dict, mode: str) -> tuple[Player, float]:
        s, side = self.s, p.side
        x, y = self.here(side)
        opp = s.other(side)
        options = []
        for t in s.lineups[side].on_pitch():
            if t.id == p.id or (s.lineups[side].is_keeper(t) and (x > 40 or mode == "long")):
                continue
            tx, ty = to_frame(side, *pos[t.id])
            d = math.hypot(tx - x, ty - y)
            if d < 4:
                continue
            free = self.e.shape.nearest(pos, opp, pos[t.id])[1]
            if mode == "long":
                w = math.exp(-(((d - 42) / 14) ** 2)) * (1.6 if GROUP[t.role] == "FWD" else 0.4)
            else:
                w = math.exp(-(((d - 15) / 10) ** 2)) * math.exp(0.035 * (tx - x))
            options.append((w * (0.3 + 0.7 * min(free / 5, 1.0)) + 1e-9, t, free))
        weights = [o[0] for o in options]
        i = int(self.rng.choice(len(options), p=[w / sum(weights) for w in weights]))
        return options[i][1], options[i][2]

    def _pass(self, a: Action) -> None:
        s, rng = self.s, self.rng
        p, side = a.player, a.player.side
        opp = s.other(side)
        pos = self.e.positions()
        start = (s.ball.x, s.ball.y)
        x, y = self.here(side)
        action_type = a.extra.get("action_type", "pass")
        restart, mode = a.extra.get("restart"), a.extra.get("mode", "short")
        if a.target is not None:
            target, free = a.target, self.e.shape.nearest(pos, opp, pos[a.target.id])[1]
        else:
            target, free = self._receiver(p, pos, mode)
        if a.end is not None:
            tx, ty = a.end
        else:
            tx, ty = to_frame(side, *pos[target.id])
            tx, ty = inside(tx + rng.normal(0.8, 1.2), ty + rng.normal(0, 1.2), 1.0)
        length = math.hypot(tx - x, ty - y)
        if a.result is not None:
            success = a.result == "success"
        else:
            press = self.e.shape.nearest(pos, opp, start)[1]
            logit = 2.85 - 0.045 * max(length - 10, 0) - 0.6 * (press < 2.0) - 0.45 * (free < 2.0) - 0.35 * (tx > 88) + 0.8 * self.edge(side)
            logit += {"kick_off": 3.0, "throw_in": 0.3, "freekick_short": 1.5, "corner_short": 2.0}.get(restart or action_type, 0.0)
            if action_type == "goalkick":
                logit += -0.3 if mode == "long" else 2.0
            elif mode == "long":
                logit -= 0.5
            success = rng.random() < _sigmoid(logit)
        bodypart = a.extra.get("bodypart") or ("other" if action_type == "throw_in" else self.foot(p))
        end = from_frame(side, tx, ty)
        if success:
            self.e.emit(p, action_type, "success", start, end, bodypart, pos=pos, restart=restart)
            s.ball.x, s.ball.y = end
            s.give(side, target)
            self.wait(a.duration if a.duration is not None else length / 15 + rng.uniform(1.45, 2.65))
            return
        self._miss(p, action_type, restart, bodypart, start, (tx, ty), length, pos)

    def _miss(self, p: Player, action_type: str, restart: str | None, bodypart: str, start, aim: tuple[float, float], length: float, pos: dict) -> None:
        """A pass that does not reach a team-mate: out of play, cut out on its way, or loose and collected."""
        s, rng = self.s, self.rng
        side = p.side
        opp = s.other(side)
        tx, ty = aim
        aim_e = from_frame(side, tx, ty)
        near_line = ty < 6 or ty > WIDTH - 6 or tx > LENGTH - 5
        r = rng.random()
        if not self.e.director.calm() and r < (0.6 if near_line else 0.12):
            out = (LENGTH + 1.0, ty) if tx > LENGTH - 5 and abs(ty - MID_Y) < 30 else (tx, -1.0 if ty < MID_Y else WIDTH + 1.0)
            end = from_frame(side, *out)
            self.e.emit(p, action_type, "fail", start, end, bodypart, pos=pos, restart=restart)
            self.wait(length / 15)
            self.e.laws.out_of_play(end, side)
            return
        if r < 0.35:
            t = float(rng.uniform(0.35, 0.85))
            point = (start[0] + t * (aim_e[0] - start[0]), start[1] + t * (aim_e[1] - start[1]))
            cutter = self.nearest_player(opp, point)
            if math.dist(pos[cutter.id], point) < 8:
                self.e.emit(p, action_type, "fail", start, point, bodypart, pos=pos, restart=restart)
                self.wait(length * t / 15 + 0.2)
                s.ball.x, s.ball.y = point
                s.give(opp, cutter)
                self.e.emit(cutter, "interception", "success", point, point, self.foot(cutter))
                self.wait(rng.uniform(0.6, 1.4))
                return
        loose = from_frame(side, *inside(tx + rng.normal(0, 3), ty + rng.normal(0, 3), 1.0))
        self.e.emit(p, action_type, "fail", start, loose, bodypart, pos=pos, restart=restart)
        self.wait(length / 15 + rng.uniform(0.5, 1.5))
        s.ball.x, s.ball.y = loose
        winner_side = side if rng.random() < 0.35 else opp
        s.give(winner_side, self.nearest_player(winner_side, loose))

    def _through_ball(self, a: Action) -> None:
        """A pass into the space behind the defence. The runner may start from an offside position: the assistant flags it, or misses a marginal one."""
        s, rng = self.s, self.rng
        p, side = a.player, a.player.side
        opp = s.other(side)
        pos = self.e.positions()
        start = (s.ball.x, s.ball.y)
        bx, _ = self.here(side)
        forwards = [t for t in s.lineups[side].outfield() if t.id != p.id]
        runner = a.target or max(forwards, key=lambda t: to_frame(side, *pos[t.id])[0])
        line = self.e.shape.line(pos, opp)
        margin = a.extra["margin"] if "margin" in a.extra else float(rng.normal(-1.0, 1.2))
        rx = max(min(line + margin, LENGTH - 2), bx + 1.0)
        margin = rx - line
        ry = to_frame(side, *pos[runner.id])[1]
        pos = dict(pos)
        pos[runner.id] = from_frame(side, rx, ry)
        if a.end is not None:
            ex, ey = a.end
        else:
            ex, ey = inside(min(line + rng.uniform(5, 13), LENGTH - 3.5), ry + rng.normal(0, 3), 4.0)
        end = from_frame(side, ex, ey)
        offside = margin > 0 and rx > max(bx, HALF_X)
        if offside:
            flagged = a.extra["flagged"] if "flagged" in a.extra else rng.random() < (0.97 if margin > 0.6 else 0.45)
            if flagged:
                ev = self.e.emit(p, "pass", "offside", start, end, self.foot(p), pos=pos, technique="through_ball", offside={"player_id": runner.id, "margin_m": round(margin, 2)})
                self.e.truth.offside(ev, runner, margin, flagged=True)
                self.wait(0.8)
                self.e.laws.offside(pos[runner.id], opp)
                return
        success = a.result == "success" if a.result is not None else rng.random() < _sigmoid(-0.6 + 0.5 * self.edge(side))
        ev = self.e.emit(p, "pass", "success" if success else "fail", start, end, self.foot(p), pos=pos, technique="through_ball")
        if offside:
            s.missed_offside = {"event_id": ev["event_id"], "player_id": runner.id, "margin_m": round(margin, 2), "at": pos[runner.id]}
            self.e.truth.offside(ev, runner, margin, flagged=False)
        length = math.hypot(ex - bx, ey - self.here(side)[1])
        self.wait(a.duration if a.duration is not None else length / 16 + rng.uniform(0.6, 1.2))
        s.ball.x, s.ball.y = end
        if success:
            s.give(side, runner)
            # the defenders are still running back when the runner gets the ball
            for d in s.lineups[opp].outfield():
                dx, dy = to_frame(side, *pos[d.id])
                self.e.shape.pin(d.id, from_frame(side, min(dx + rng.uniform(1.5, 4.0), LENGTH - 1), dy), steps=2)
            return
        r = rng.random()
        if r < 0.35:
            keeper = s.lineups[opp].keeper()
            s.give(opp, keeper)
            self.e.emit(keeper, "keeper_claim", "success", end, end, "other")
            self.wait(rng.uniform(2, 5))
        elif r < 0.85 or self.e.director.calm():
            s.give(opp, self.nearest_player(opp, end))
        else:
            self.e.laws.out_of_play(from_frame(side, LENGTH + 1.0, ey), side)

    def _cross(self, a: Action) -> None:
        s, rng = self.s, self.rng
        p, side = a.player, a.player.side
        opp = s.other(side)
        pos = self.e.positions()
        start = (s.ball.x, s.ball.y)
        x, y = self.here(side)
        action_type, restart = a.extra.get("action_type", "cross"), a.extra.get("restart")
        tx, ty = a.end or (float(rng.uniform(92, 99)), min(max(float(rng.normal(MID_Y, 5)), 26.0), 42.0))
        end = from_frame(side, tx, ty)
        target = a.target or self.nearest_player(side, end, exclude=p.id, outfield=True)
        if a.result is not None:
            success = a.result == "success"
        else:
            success = rng.random() < 0.24 + 0.05 * self.edge(side) + (0.06 if restart == "corner" else 0.0)
        self.e.emit(p, action_type, "success" if success else "fail", start, end, a.extra.get("bodypart") or self.foot(p), pos=pos, restart=restart)
        self.wait(math.hypot(tx - x, ty - y) / 18 + 0.4)
        s.ball.x, s.ball.y = end
        header = rng.random() < 0.38
        if success:
            s.give(side, target)
            cap = self.e.director.shot_cap()
            if header and (cap is None or xg(tx, ty, header=True) <= cap) and not self.e.director.calm():
                self._shot(Action("shot", target, extra={"header": True}))
            return
        r = rng.random()
        if r < 0.22:
            keeper = s.lineups[opp].keeper()
            s.give(opp, keeper)
            self.e.emit(keeper, "keeper_claim", "success", end, end, "other")
            self.wait(rng.uniform(2, 6))
        elif r < 0.75 or self.e.director.calm():
            defender = self.nearest_player(opp, end, outfield=True)
            s.give(opp, defender)
            self._clearance(Action("clearance", defender, extra={"header": header}))
        elif rng.random() < 0.55:
            self.e.laws.order("corner", side, *self.e.laws.corner_spot(side, ty), delay=rng.uniform(18, 32))
        else:
            self.e.laws.out_of_play(from_frame(side, LENGTH + 1.0, ty), side)

    # --- carrying the ball ----------------------------------------------------------------------

    def _dribble(self, a: Action) -> None:
        """A carry (SPADL "dribble"): the player runs with the ball."""
        s, rng = self.s, self.rng
        p, side = a.player, a.player.side
        pos = self.e.positions()
        start = (s.ball.x, s.ball.y)
        x, y = self.here(side)
        if a.end is not None:
            nx, ny = a.end
        else:
            d = float(rng.uniform(3, 11) if x < 75 else rng.uniform(2, 5))
            aim = math.atan2(MID_Y - y, LENGTH - x) if x > 60 else 0.0
            theta = 0.3 * aim + float(rng.normal(0, 0.55))
            nx, ny = inside(min(x + d * math.cos(theta), LENGTH - 2), y + d * math.sin(theta), 1.5)
            line = self.e.shape.line(pos, s.other(side))
            if x < line - 1.5 < nx:  # nobody carries the ball through a set defensive line: he stops at it and must beat a man
                nx = line - 1.5
        end = from_frame(side, nx, ny)
        self.e.emit(p, "dribble", "success", start, end, self.foot(p), pos=pos)
        s.ball.x, s.ball.y = end
        self.wait(a.duration if a.duration is not None else math.hypot(nx - x, ny - y) / 5.5 + rng.uniform(0.3, 0.9))
        if a.result is None and not self.e.director.calm():
            opp = s.other(side)
            defender = self.nearest_player(opp, end)
            press = math.dist(self.e.positions()[defender.id], end)
            if rng.random() < 0.06 + 0.12 * (press < 2.0) + 0.08 * (nx > 80) + 0.15 * (nx > 88) - 0.02 * self.edge(side):
                self._challenge(p, defender)

    def _challenge(self, attacker: Player, defender: Player) -> None:
        """A defender goes in: he wins the ball, or fouls."""
        s, rng = self.s, self.rng
        at = (s.ball.x, s.ball.y)
        if rng.random() < 0.4 * self.foul_odds(attacker, at):
            if rng.random() < 0.2:  # the attacker is penalised: a push, a trip on the way through
                s.give(defender.side, defender)
                self.e.laws.foul(attacker, defender, at)
            else:
                self.e.laws.foul(defender, attacker, at)
            return
        if rng.random() < 0.12 and not self.e.director.calm():
            x, y = self.here(attacker.side)
            self.e.emit(defender, "tackle", "success", at, at, self.foot(defender))
            self.e.laws.out_of_play(from_frame(attacker.side, x, -1.0 if y < MID_Y else WIDTH + 1.0), defender.side)
            return
        s.give(defender.side, defender)
        self.e.emit(defender, "tackle", "success", at, at, self.foot(defender))
        self.wait(rng.uniform(0.6, 1.4))

    def _take_on(self, a: Action) -> None:
        s, rng = self.s, self.rng
        p, side = a.player, a.player.side
        opp = s.other(side)
        pos = self.e.positions()
        start = (s.ball.x, s.ball.y)
        x, y = self.here(side)
        defender = self.nearest_player(opp, start)
        if rng.random() < 0.5 + 0.12 * self.edge(side):
            end = from_frame(side, *inside(min(x + rng.uniform(3, 6), LENGTH - 2), y + rng.normal(0, 2), 1.5))
            self.e.emit(p, "take_on", "success", start, end, self.foot(p), pos=pos)
            s.ball.x, s.ball.y = end
            self.wait(rng.uniform(1.0, 2.0))
            return
        self.e.emit(p, "take_on", "fail", start, start, self.foot(p), pos=pos)
        self.wait(rng.uniform(0.6, 1.2))
        r = rng.random()
        if r < 0.45 * self.foul_odds(p, start):
            self.e.laws.foul(defender, p, start)
        elif r > 0.93 and not self.e.director.calm():
            self.e.laws.out_of_play(from_frame(side, x, -1.0 if y < MID_Y else WIDTH + 1.0), opp)
        else:
            s.give(opp, defender)
            self.e.emit(defender, "tackle", "success", start, start, self.foot(defender))
            self.wait(rng.uniform(0.6, 1.4))

    def _clearance(self, a: Action) -> None:
        s, rng = self.s, self.rng
        p, side = a.player, a.player.side
        opp = s.other(side)
        start = (s.ball.x, s.ball.y)
        x, y = self.here(side)
        nx, ny = x + float(rng.uniform(25, 50)), y + float(rng.normal(0, 14))
        out = (ny < 0 or ny > WIDTH) and not self.e.director.calm()
        if not out:
            nx, ny = min(nx, LENGTH - 5), min(max(ny, 1.0), WIDTH - 1.0)
        else:
            ny = -1.0 if ny < 0 else WIDTH + 1.0
        end = from_frame(side, nx, ny)
        self.e.emit(p, "clearance", "success", start, end, "head" if a.extra.get("header") else self.foot(p))
        self.wait(math.hypot(nx - x, ny - y) / 20 + rng.uniform(1.0, 2.0))
        if out:
            self.e.laws.out_of_play(end, side)
            return
        s.ball.x, s.ball.y = end
        winner_side = side if rng.random() < 0.35 else opp
        s.give(winner_side, self.nearest_player(winner_side, end))

    # --- shots ----------------------------------------------------------------------------------

    def _shot(self, a: Action) -> None:
        s, rng = self.s, self.rng
        p, side = a.player, a.player.side
        moment = a.extra.get("moment")
        for pid, xy in a.extra.get("pins", {}).items():
            self.e.shape.pin(pid, from_frame(side, *xy), steps=1)
        pos = self.e.positions()
        start = (s.ball.x, s.ball.y)
        x, y = self.here(side)
        action_type, restart = a.extra.get("action_type", "shot"), a.extra.get("restart")
        header = a.extra.get("header", False)
        if action_type == "shot_penalty":
            chance = PENALTY_XG
        else:
            chance = xg(x, y, header=header, blockers=self.e.shape.blockers(pos, side, (x, y)))
        outcome = a.result or self._outcome(chance, (x, y), action_type)
        bodypart = "head" if header else a.extra.get("bodypart") or self.foot(p)
        (ex, ey), z = self._shot_end(outcome, (x, y))
        score_before = dict(s.score)
        ev = self.e.emit(
            p, action_type, "success" if outcome == "goal" else "fail", start, from_frame(side, ex, ey), bodypart,
            pos=pos, restart=restart, shot={"xg": round(chance, 3), "outcome": outcome, "end_z": round(z, 2)},
        )
        self.e.truth.shot(ev, moment, score_before, dict(s.reds))
        if moment is not None:
            self.e.truth.moment(moment, ev)
        self.wait(rng.uniform(0.5, 1.0))
        opp = s.other(side)
        if outcome == "goal":
            self.e.laws.goal(ev, side, a)
        elif outcome == "saved":
            self._save(p, (ex, ey), a.extra.get("keeper"))
        elif outcome == "off_target":
            self.e.laws.out_of_play(from_frame(side, LENGTH + 1.0, ey), side)
        elif outcome == "post":
            rebound = from_frame(side, *inside(LENGTH - rng.uniform(4, 10), y + rng.normal(0, 5), 2.0))
            s.ball.x, s.ball.y = rebound
            winner_side = side if rng.random() < 0.4 else opp
            s.give(winner_side, self.nearest_player(winner_side, rebound))
        else:  # blocked
            block = from_frame(side, ex, ey)
            s.ball.x, s.ball.y = block
            if rng.random() < 0.3 and not self.e.director.calm():
                self.e.laws.order("corner", side, *self.e.laws.corner_spot(side, ey), delay=rng.uniform(18, 32))
            else:
                winner_side = side if rng.random() < 0.35 else opp
                s.give(winner_side, self.nearest_player(winner_side, block))

    def _outcome(self, chance: float, at: tuple[float, float], action_type: str) -> str:
        rng = self.rng
        scoring = self.e.director.background_goals
        if rng.random() < chance:
            return "goal" if scoring else "saved"
        if action_type == "shot_penalty":
            return "saved" if rng.random() < 0.7 else "off_target"
        p_block = 0.18 + (0.12 if not in_box(*at) else 0.0)
        r = rng.random()
        if r < p_block:
            return "blocked"
        if r < p_block + 0.03:
            return "post"
        return "saved" if rng.random() < (0.45 if in_box(*at) else 0.33) else "off_target"

    def _shot_end(self, outcome: str, at: tuple[float, float]) -> tuple[tuple[float, float], float]:
        rng = self.rng
        x, y = at
        if outcome in ("goal", "saved"):
            return (LENGTH, float(rng.uniform(POSTS[0] + 0.2, POSTS[1] - 0.2))), float(rng.uniform(0.1, 2.3))
        if outcome == "post":
            return (LENGTH, POSTS[0] if rng.random() < 0.5 else POSTS[1]), float(rng.uniform(0.1, 2.4))
        if outcome == "off_target":
            if rng.random() < 0.45:
                return (LENGTH, float(rng.uniform(POSTS[0] - 1.5, POSTS[1] + 1.5))), float(rng.uniform(2.5, 4.5))
            wide = POSTS[0] - rng.uniform(0.3, 4) if rng.random() < 0.5 else POSTS[1] + rng.uniform(0.3, 4)
            return (LENGTH, float(wide)), float(rng.uniform(0.1, 2.0))
        dist = math.hypot(LENGTH - x, MID_Y - y)
        d = min(float(rng.uniform(1.0, 4.0)), dist / 2)  # blocked a few metres out
        return inside(x + d * (LENGTH - x) / dist, y + d * (MID_Y - y) / dist, 0.5), 0.5

    def _save(self, shooter: Player, end: tuple[float, float], forced: str | None) -> None:
        s, rng = self.s, self.rng
        side = shooter.side
        opp = s.other(side)
        keeper = s.lineups[opp].keeper()
        at = from_frame(opp, 0.8, WIDTH - end[1])
        s.ball.x, s.ball.y = at
        s.give(opp, keeper)
        self.e.emit(keeper, "keeper_save", "success", at, at, "other")
        r = rng.random()
        if forced == "parry_corner" or (forced is None and r < 0.25 and not self.e.director.calm()):
            self.e.laws.order("corner", side, *self.e.laws.corner_spot(side, end[1]), delay=rng.uniform(20, 32))
        elif forced is None and r < 0.45:
            loose = from_frame(opp, *inside(rng.uniform(4, 11), WIDTH - end[1] + rng.normal(0, 6), 2.0))
            s.ball.x, s.ball.y = loose
            winner_side = side if rng.random() < 0.5 else opp
            s.give(winner_side, self.nearest_player(winner_side, loose))
            self.wait(rng.uniform(0.8, 1.6))
        else:
            self.wait(rng.uniform(3, 8))  # the keeper holds the ball before distributing it

    # --- planted actions ------------------------------------------------------------------------

    def _recover(self, a: Action) -> None:
        """The scripted team wins the ball back, as a tackle or an interception."""
        s = self.s
        at = (s.ball.x, s.ball.y)
        s.give(a.player.side, a.player)
        kind = "interception" if self.rng.random() < 0.5 else "tackle"
        self.e.emit(a.player, kind, "success", at, at, self.foot(a.player))
        self.wait(a.duration if a.duration is not None else 1.0)

    def _foul(self, a: Action) -> None:
        s = self.s
        for pid, xy in a.extra.get("pins", {}).items():
            self.e.shape.pin(pid, from_frame(s.ball.owner.side, *xy), steps=1)
        self.e.laws.foul(a.player, s.ball.owner, (s.ball.x, s.ball.y), card=a.extra.get("card"), reason=a.extra.get("reason"), moment=a.extra.get("moment"))
