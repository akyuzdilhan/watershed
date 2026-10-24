"""The referee: every rule of the game lives here.

Out of play and restarts, fouls, penalties and cards (a second yellow is a red), offside, goals and the VAR protocol, substitutions and injuries at dead balls, added time and the end of each half. Laws.check asserts the invariants after every engine step.
"""

from generator.actions import Action
from generator.pitch import CENTRE, LENGTH, MID_Y, PENALTY_SPOT, WIDTH, from_frame, goal_distance, in_box, to_frame
from generator.state import Restart
from generator.teams import GROUP, Player

YELLOW_PER_FOUL = 0.17  # about 4.2 yellow cards for 25 fouls a match
SLOW_RESTART = 0.12  # share of every restart delay the referee adds back (time wasting, slow restarts)
CELEBRATION_FREE_S = 30.0  # since 2024/25 the Premier League adds back only what a goal celebration takes beyond 30 seconds
VAR_REASONS = ["possible offside", "possible foul", "possible handball"]


class Laws:
    def __init__(self, engine) -> None:
        self.e = engine
        self.rng = engine.rng
        self.sub_plan: dict[str, list[list[float]]] = {"home": [], "away": []}  # [match clock, players] for each substitution window

    @property
    def s(self):
        return self.e.state

    # --- restarts -------------------------------------------------------------------------------

    def order(self, kind: str, side: str, x: float, y: float, delay: float = 0.0, lost: float = 0.0, direct: bool = True) -> None:
        """The ball is dead: `side` restarts from (x, y), engine frame, after `delay` seconds, `lost` of which are added back at the end of the half."""
        s = self.s
        s.ball.alive, s.ball.owner, s.phase = False, None, "stopped"
        s.ball.restart = Restart(kind, side, float(x), float(y), direct, float(delay), float(lost))

    def corner_spot(self, side: str, y_team: float) -> tuple[float, float]:
        return from_frame(side, LENGTH, 0.0 if y_team < MID_Y else WIDTH)

    def out_of_play(self, end: tuple[float, float], last_side: str) -> None:
        """The ball crossed a line at `end` (engine frame), last touched by `last_side`."""
        s, rng = self.s, self.rng
        x, y = end
        if y < 0 or y > WIDTH:
            self.order("throw_in", s.other(last_side), min(max(x, 1.0), LENGTH - 1.0), 0.0 if y < 0 else WIDTH, delay=rng.uniform(8, 16))
            return
        attacking = "home" if x > LENGTH else "away"  # the team that attacks that goal line
        if last_side == attacking:
            defending = s.other(attacking)
            self.order("goal_kick", defending, *from_frame(defending, 5.5, MID_Y + rng.uniform(-9, 9)), delay=rng.uniform(14, 26))
        else:
            self.order("corner", attacking, *self.corner_spot(attacking, to_frame(attacking, x, y)[1]), delay=rng.uniform(18, 32))

    def restart(self) -> None:
        s = self.s
        self.dead_ball()
        r = s.ball.restart
        lost = min(r.lost_s + SLOW_RESTART * r.delay_s, r.delay_s)
        s.clock.advance(r.delay_s - lost)
        s.clock.advance(lost, stoppage=True)
        end = s.clock.end_s
        if end is not None and s.clock.clock_s >= end:
            return  # time is up while the ball is dead: the referee blows without the restart
        s.ball.x, s.ball.y, s.ball.restart = r.x, r.y, None
        s.ball.alive, s.phase, s.missed_offside = True, "in_play", None
        s.give(r.side, None)
        taker = self.taker(r)
        s.ball.owner = taker
        self.e.actions.resolve(self.restart_action(r, taker))

    def taker(self, r: Restart) -> Player:
        lineup = self.s.lineups[r.side]

        def first(*roles: str) -> Player | None:
            return next((p for role in roles for p in lineup.on_pitch() if p.role == role), None)

        if r.kind == "goal_kick":
            return lineup.keeper()
        if r.kind == "penalty":
            return first("ST", "AM", "RW", "LW", "CM") or lineup.outfield()[-1]
        if r.kind == "kick_off":
            return first("ST", "AM", "CM") or lineup.outfield()[-1]
        if r.kind == "corner":
            options = [p for p in lineup.on_pitch() if p.role in ("RW", "LW", "AM", "CM")] or lineup.outfield()
            return options[int(self.rng.integers(len(options)))]
        return self.e.actions.nearest_player(r.side, (r.x, r.y), outfield=r.kind == "throw_in")

    def restart_action(self, r: Restart, taker: Player) -> Action:
        rng = self.rng
        x, y = to_frame(r.side, r.x, r.y)
        calm = self.e.director.calm()
        extra = {"restart": r.kind}
        if r.kind == "kick_off":
            target = self.e.actions.nearest_player(r.side, from_frame(r.side, 40.0, MID_Y), exclude=taker.id, outfield=True)
            return Action("pass", taker, target=target, extra=extra)
        if r.kind == "throw_in":
            return Action("pass", taker, extra={**extra, "action_type": "throw_in"})
        if r.kind == "goal_kick":
            return Action("pass", taker, extra={**extra, "action_type": "goalkick", "mode": "long" if rng.random() < 0.55 else "short"})
        if r.kind == "corner":
            if not calm and rng.random() < 0.85:
                return Action("cross", taker, extra={**extra, "action_type": "corner_crossed"})
            return Action("pass", taker, extra={**extra, "action_type": "corner_short"})
        if r.kind == "penalty":
            return Action("shot", taker, extra={**extra, "action_type": "shot_penalty"})
        if r.direct and not calm and goal_distance(x, y) < 30 and abs(y - MID_Y) < 20 and rng.random() < 0.4:
            return Action("shot", taker, extra={**extra, "action_type": "shot_freekick"})
        if r.direct and not calm and x > 68:
            return Action("cross", taker, extra={**extra, "action_type": "freekick_crossed"})
        return Action("pass", taker, extra={**extra, "action_type": "freekick_short"})

    # --- fouls, cards, offside ------------------------------------------------------------------

    def foul(self, offender: Player, victim: Player, at: tuple[float, float], card: str | None = None, reason: str | None = None, moment=None) -> None:
        s, rng = self.s, self.rng
        s.ball.x, s.ball.y = at
        s.give(victim.side, victim)
        card = card or self._card(offender)
        result = {"yellow": "yellow_card", "second_yellow": "red_card", "red": "red_card"}.get(card, "fail")
        ev = self.e.emit(offender, "foul", result, at, at, "foot", card=card, card_reason=reason)
        if moment is not None:
            self.e.truth.moment(moment, ev)
        lost = 0.0
        if card:
            self.e.truth.card(ev, card)
            lost = float(rng.uniform(15, 25))
            if card == "yellow":
                s.yellows[offender.id] = s.yellows.get(offender.id, 0) + 1
            else:
                lineup = s.lineups[offender.side]
                lineup.send_off(offender)
                s.reds[offender.side] += 1
                if lineup.slots[0] is None:  # the keeper is sent off
                    mover, keeper = lineup.replace_keeper()
                    if keeper is not None:
                        self.e.message("substitution", team=offender.side, player_out=mover.ref(), player_in=keeper.ref())
        if in_box(*to_frame(victim.side, *at)):
            self.order("penalty", victim.side, *from_frame(victim.side, *PENALTY_SPOT), delay=lost + rng.uniform(55, 85), lost=lost + rng.uniform(30, 50))
        else:
            wall = to_frame(victim.side, *at)[0] > 70
            self.order("free_kick", victim.side, *at, delay=lost + (rng.uniform(25, 50) if wall else rng.uniform(10, 24)), lost=lost)

    def _card(self, offender: Player) -> str | None:
        r = self.rng.random()
        booked = self.s.yellows.get(offender.id, 0) >= 1
        if not self.e.director.background_reds:
            return None if booked or self.e.director.protected(offender.id) or r >= YELLOW_PER_FOUL else "yellow"
        if r < 0.002:
            return "red"
        if booked:
            return "second_yellow" if r < 0.045 else None
        return "yellow" if r < YELLOW_PER_FOUL else None

    def offside(self, at: tuple[float, float], defending: str) -> None:
        """Indirect free kick for the defending team, where the offside player stood when the ball was played."""
        self.order("free_kick", defending, *at, delay=self.rng.uniform(8, 15), direct=False)

    # --- goals and VAR --------------------------------------------------------------------------

    def goal(self, ev: dict, side: str, a: Action) -> None:
        s, rng = self.s, self.rng
        s.score[side] += 1
        ev["score"] = dict(s.score)
        moment = a.extra.get("moment")
        self.e.truth.goal(ev, moment)
        s.ball.alive, s.ball.owner, s.phase = False, None, "stopped"
        var = a.extra.get("var")
        if var is None and self.e.director.background_goals:
            if s.missed_offside:
                var = {"after_s": rng.uniform(12, 25), "duration_s": rng.uniform(45, 110), "decision": "overturned", "reason": "offside"}
            elif rng.random() < 0.1:
                var = {"after_s": rng.uniform(10, 20), "duration_s": rng.uniform(25, 80), "decision": "stands", "reason": VAR_REASONS[int(rng.integers(3))]}
        celebration = float(rng.uniform(45, 70))
        if var is not None and var["decision"] == "overturned":
            self.var_check(ev, side, var)
            return
        if var is not None:
            self.var_check(ev, side, var)
            rest = max(celebration - var["after_s"] - var["duration_s"], 0.0) + float(rng.uniform(10, 20))
        else:
            rest = celebration
        self._credit(ev, side, moment)
        self.order("kick_off", s.other(side), *CENTRE, delay=rest, lost=max(rest - CELEBRATION_FREE_S, 0.0))

    def _credit(self, ev: dict, side: str, moment) -> None:
        """A goal that stands may later be credited to another player (a deflection), as providers do after review."""
        rng = self.rng
        if moment is not None and moment.correction:
            c = moment.correction
            to = self.s.teams[side].player(c["to"])
            self.e.schedule(ev["wall_s"] + c["after_s"], "reassign", ev["event_id"], {"player": to.ref()}, c["cause"], note=c.get("note"))
        elif moment is None and self.e.director.background_goals and rng.random() < 0.05:
            others = [p for p in self.s.lineups[side].outfield() if p.id != ev["player"]["id"]]
            to = others[int(rng.integers(len(others)))]
            self.e.schedule(ev["wall_s"] + rng.uniform(60, 180), "reassign", ev["event_id"], {"player": to.ref()}, "deflection")

    def var_check(self, ev: dict, side: str, var: dict) -> None:
        """Play is stopped while the video assistant reviews the goal; an overturned goal is voided and play restarts with a free kick."""
        s, rng = self.s, self.rng
        s.phase = "var_check"
        s.clock.advance(var["after_s"], stoppage=True)
        check_id = f"{self.e.match_id}-v2-var{len(self.e.truth.var_checks) + 1:02d}"
        fields = {"check_id": check_id, "target_event_id": ev["event_id"], "incident": "goal", "reason": var["reason"]}
        started = self.e.message("var_check", status="started", **fields)
        s.clock.advance(var["duration_s"], stoppage=True)
        done = self.e.message("var_check", status="completed", decision=var["decision"], **fields)
        self.e.truth.var_check(started, done)
        if var["decision"] != "overturned":
            return
        s.score[side] -= 1
        delay = float(rng.uniform(15, 30))
        if var.get("offence") == "handball":  # handball by the scorer: direct free kick to the defenders where he touched it
            self.e.correct("void", ev["event_id"], {}, "var_handball", related=done["message_id"])
            at = from_frame(side, ev["start_x"], ev["start_y"])
            self.order("free_kick", s.other(side), *at, delay=delay, lost=delay)
            return
        off = s.missed_offside
        self.e.correct("update", off["event_id"], {"result": "offside", "offside": {"player_id": off["player_id"], "margin_m": off["margin_m"]}}, "var_offside", related=done["message_id"])
        self.e.correct("void", ev["event_id"], {}, "var_offside", related=done["message_id"])
        s.missed_offside = None
        self.order("free_kick", s.other(side), *off["at"], delay=delay, lost=delay, direct=False)

    # --- dead balls: substitutions and injuries -------------------------------------------------

    def plan_substitutions(self) -> None:
        """Three to five substitutions per team in at most three windows, between the 56th and the 88th minute [Premier League rule: five substitutes, three windows]."""
        rng = self.rng
        for side in ("home", "away"):
            n = int(rng.choice([3, 4, 5], p=[0.25, 0.35, 0.4]))
            counts = [1, 1, 1]
            for _ in range(n - 3):
                counts[int(rng.integers(3))] += 1
            times = sorted(float(t) for t in rng.uniform(56 * 60, 88 * 60, size=3))
            self.sub_plan[side] = [[t, c] for t, c in zip(times, counts)]
            if rng.random() < 0.2:
                self.substitute(side, 1, window=False)  # at half-time: no window used

    def dead_ball(self) -> None:
        s, rng = self.s, self.rng
        if self.e.director.calm():
            return
        if s.clock.period == 2:
            for side in ("home", "away"):
                plan = self.sub_plan[side]
                if plan and s.clock.clock_s >= plan[0][0]:
                    self.substitute(side, int(plan.pop(0)[1]))
        if rng.random() < 0.012:
            side = "home" if rng.random() < 0.5 else "away"
            players = s.lineups[side].outfield()
            p = players[int(rng.integers(len(players)))]
            self.e.message("stoppage", reason="injury", team=side, player=p.ref())
            s.clock.advance(rng.uniform(40, 110), stoppage=True)

    def substitute(self, side: str, count: int, window: bool = True) -> None:
        s, rng = self.s, self.rng
        lineup = s.lineups[side]
        count = min(count, 5 - lineup.subs_used)
        if count <= 0 or (window and lineup.windows_used >= 3):
            return
        done = 0
        for _ in range(count):
            starters = {p.id for p in lineup.team.squad[:11]}
            options = [p for p in lineup.outfield() if p.id in starters and not self.e.director.protected(p.id)]
            if not options:
                break
            weights = [{"FWD": 3.0, "MID": 2.0, "DEF": 1.0}[GROUP[p.role]] + 2.0 * (s.yellows.get(p.id, 0) > 0) for p in options]
            out = options[int(rng.choice(len(options), p=[w / sum(weights) for w in weights]))]
            bench = [b for b in lineup.bench if GROUP[b.role] == GROUP[out.role]] or [b for b in lineup.bench if b.role != "GK"]
            if not bench:
                break
            incoming = bench[int(rng.integers(len(bench)))]
            lineup.substitute(out, incoming)
            self.e.message("substitution", team=side, player_out=out.ref(), player_in=incoming.ref())
            done += 1
        if done and window:  # half-time changes use no window and no playing time
            lineup.windows_used += 1
            s.clock.advance(15 + 8 * done + float(rng.uniform(0, 5)), stoppage=True)

    # --- time -----------------------------------------------------------------------------------

    def half_over(self) -> bool:
        """Announces the added time at the end of regulation, then tells when the referee ends the half."""
        c = self.s.clock
        if c.added_min[c.period] is None and c.clock_s >= c.regulation_end:
            minutes = c.announce()
            c.over_s[c.period] = float(self.rng.uniform(0, 30))
            self.e.message("added_time", minutes=minutes)
        end = c.end_s
        return end is not None and c.clock_s >= end and not self.e.director.plan

    def check(self) -> None:
        s = self.s
        for side in ("home", "away"):
            assert len(s.lineups[side].on_pitch()) == 11 - s.reds[side], f"{side} has the wrong number of players"
        if s.ball.alive:
            owner = s.ball.owner
            assert owner is not None and s.lineups[owner.side].is_on(owner), "live ball without a player on the pitch"
            assert owner.side == s.possession, "the ball carrier is not in the team in possession"
            assert 0 <= s.ball.x <= LENGTH and 0 <= s.ball.y <= WIDTH, f"live ball off the pitch at {s.ball.x, s.ball.y}"
        else:
            assert s.ball.restart is not None or s.phase in ("half_time", "full_time"), "dead ball without a restart"
        assert min(s.score.values()) >= 0
