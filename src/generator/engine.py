"""The match engine: a state machine stepped until the final whistle.

Each step either restarts a dead ball (Laws) or plays the next action of the player on the ball (Director, else ActionModel), then checks the invariants. Events go to an ordered message log; build_stream turns it into the feed and TruthRecorder into the hidden ground truth.
"""

import numpy as np

from generator.actions import ActionModel
from generator.director import DEMO_SCRIPT, Director
from generator.feed import ID_TAG, TruthRecorder, build_stream
from generator.laws import Laws
from generator.pitch import CENTRE, LENGTH, WIDTH, to_frame
from generator.shape import Shape
from generator.state import MatchState
from generator.teams import FORMATIONS, Lineup, Player, make_teams

STORIES = ("demo", "random")
KICKOFF_UTC = "2026-10-24T14:00:00Z"
PROVIDER_REVIEW = 0.0015  # chance that a pass gets a minor correction from the provider (end location fixed a minute or two later)


class MatchEngine:
    def __init__(self, seed: int, story: str = "random", match_id: str | None = None, frames: bool = True) -> None:
        if story not in STORIES:
            raise ValueError(f"story must be one of {STORIES}, not {story!r}")
        self.rng = np.random.default_rng(seed)
        self.seed, self.story, self.frames = seed, story, frames
        self.match_id = match_id or f"{'demo' if story == 'demo' else 'synthetic'}-{seed:04d}"
        if story == "demo":
            home_xg, away_xg = 1.5, 1.2  # the defaults of the win-probability model
        else:
            home_xg, away_xg = 1.5 * float(np.exp(self.rng.normal(0, 0.25))), 1.2 * float(np.exp(self.rng.normal(0, 0.25)))
        home, away, self.referee = make_teams(self.rng, home_xg, away_xg)
        teams = {"home": home, "away": away}
        self.state = MatchState(teams=teams, lineups={side: Lineup(t) for side, t in teams.items()})
        self.shape = Shape(self.rng, self.state)
        self.actions = ActionModel(self)
        self.laws = Laws(self)
        self.director = Director(self, DEMO_SCRIPT if story == "demo" else [])
        self.truth = TruthRecorder(self)
        self.messages: list[dict] = []
        self.scheduled: list[tuple[float, int, dict]] = []
        self.versions: dict[str, int] = {}
        self.n_actions = self.n_controls = self.n_corrections = 0
        self._pos: dict | None = None
        self._pos_key: tuple | None = None

    # --- playing --------------------------------------------------------------------------------

    def run(self) -> None:
        s = self.state
        self.message("match_info", **self.match_info())
        for period in (1, 2):
            s.clock.start(period)
            self.message("period_start")
            if period == 2:
                self.laws.plan_substitutions()
            self.laws.order("kick_off", "home" if period == 1 else "away", *CENTRE)
            while not self.laws.half_over():
                self.step()
            self.message("period_end", score=dict(s.score))
            s.ball.alive, s.ball.owner, s.ball.restart, s.phase = False, None, None, "half_time"
        self.flush_corrections(force=True)
        s.phase = "full_time"
        self.message("full_time", score=dict(s.score))

    def step(self) -> None:
        self.shape.tick()
        self.flush_corrections()
        if not self.state.ball.alive:
            self.laws.restart()
        else:
            self.actions.resolve(self.director.next() or self.actions.choose())
        self.laws.check()

    def positions(self) -> dict[str, tuple[float, float]]:
        """Positions of the 22 players for the current ball location, computed once per location."""
        s = self.state
        key = (s.ball.x, s.ball.y, s.possession, s.ball.owner.id if s.ball.owner else None, self.shape.version, s.lineups["home"].subs_used, s.lineups["away"].subs_used, s.reds["home"], s.reds["away"])
        if key != self._pos_key:
            self._pos, self._pos_key = self.shape.positions(), key
        return self._pos

    def player(self, player_id: str) -> Player:
        return next(p for t in self.state.teams.values() for p in t.squad if p.id == player_id)

    # --- the message log ------------------------------------------------------------------------

    def message(self, type_: str, message_id: str | None = None, **payload) -> dict:
        """A feed message (anything but an action), stamped with both clocks."""
        s = self.state
        if message_id is None:
            self.n_controls += 1
            message_id = f"{self.match_id}-{ID_TAG}-m{self.n_controls:04d}"
        m = {"type": type_, "message_id": message_id, "wall_s": round(s.clock.wall_s, 2)}
        if type_ != "match_info":
            m.update(s.clock.fields())
        m.update({k: v for k, v in payload.items() if v is not None})
        self.messages.append(m)
        return m

    def emit(self, player: Player, action_type: str, result: str, start: tuple[float, float], end: tuple[float, float], bodypart: str, pos: dict | None = None, **extra) -> dict:
        """An action by `player` from `start` to `end` (engine frame), with the freeze frame at its start."""
        s = self.state
        side = player.side
        assert s.lineups[side].is_on(player), f"{player.id} is not on the pitch"
        if pos is None:
            pos = self.positions()
        sx, sy = to_frame(side, *start)
        ex, ey = to_frame(side, *end)
        self.n_actions += 1
        event_id = f"{self.match_id}-{ID_TAG}-a{self.n_actions:05d}"
        ev = {
            "type": "action", "message_id": event_id, "wall_s": round(s.clock.wall_s, 2), **s.clock.fields(),
            "event_id": event_id, "version": 1, "possession": s.possession_id,
            "team": side, "team_name": s.teams[side].name, "player": player.ref(),
            "action_type": action_type, "result": result, "bodypart": bodypart,
            "start_x": round(sx, 1), "start_y": round(sy, 1),
            "end_x": round(min(max(ex, 0.0), LENGTH), 1), "end_y": round(min(max(ey, 0.0), WIDTH), 1),
            "under_pressure": self.shape.nearest(pos, s.other(side), start)[1] < 2.5,
        }
        ev.update({k: v for k, v in extra.items() if v is not None})
        ev["score"] = dict(s.score)
        if self.frames:
            ev["freeze_frame"] = self.shape.frame(pos, side, player.id)
        self.messages.append(ev)
        if action_type == "pass" and not self.director.plan and self.rng.random() < PROVIDER_REVIEW:
            fixed = {"end_x": round(min(max(ev["end_x"] + float(self.rng.normal(0, 3)), 0.0), LENGTH), 1), "end_y": round(min(max(ev["end_y"] + float(self.rng.normal(0, 3)), 0.0), WIDTH), 1)}
            self.schedule(s.clock.wall_s + float(self.rng.uniform(60, 180)), "update", event_id, fixed, "provider_review")
        return ev

    def correct(self, op: str, target: str, changes: dict, cause: str, related: str | None = None, note: str | None = None) -> dict:
        """A correction message on an earlier action: void, reassign or update."""
        version = self.versions.get(target, 1)
        self.n_corrections += 1
        msg = self.message(
            "correction", message_id=f"{self.match_id}-{ID_TAG}-c{self.n_corrections:03d}",
            op=op, target_event_id=target, target_version=version, new_version=version + 1, cause=cause, changes=changes,
            related_message_id=related, note=note, score=dict(self.state.score),
        )
        self.versions[target] = version + 1
        self.truth.correction(msg)
        return msg

    def schedule(self, wall_s: float, op: str, target: str, changes: dict, cause: str, note: str | None = None) -> None:
        """A correction the provider sends later, on the broadcast clock."""
        self.scheduled.append((wall_s, len(self.scheduled), {"op": op, "target": target, "changes": changes, "cause": cause, "note": note}))

    def flush_corrections(self, force: bool = False) -> None:
        now = self.state.clock.wall_s
        due = sorted(c for c in self.scheduled if force or c[0] <= now)
        for c in due:
            self.scheduled.remove(c)
            self.correct(**c[2])

    def match_info(self) -> dict:
        teams = {}
        for side, t in self.state.teams.items():
            teams[side] = {
                "name": t.name,
                "formation": t.formation,
                "expected_goals": round(t.expected_goals, 2),
                "attacks_towards": {"1": "right", "2": "left"} if side == "home" else {"1": "left", "2": "right"},
                "starting": [{**p.ref(), "role": FORMATIONS[t.formation][i][0], "foot": p.foot} for i, p in enumerate(t.squad[:11])],
                "bench": [{**p.ref(), "role": p.role, "foot": p.foot} for p in t.squad[11:]],
            }
        return {
            "competition": "Synthetic League",
            "story": self.story,
            "kickoff_utc": KICKOFF_UTC,
            "referee": {"name": self.referee},
            "pitch": {"length_m": LENGTH, "width_m": WIDTH, "frame": "locations are seen from the acting team, which attacks towards x = 105 with y = 0 on its right"},
            "freeze_frames": self.frames,
            "teams": teams,
        }


def generate_match(seed: int = 7, story: str = "random", match_id: str | None = None, frames: bool = True) -> tuple[list[dict], dict]:
    """Returns the feed (messages in arrival order) and the hidden ground truth."""
    engine = MatchEngine(seed, story, match_id, frames)
    engine.run()
    stream, incidents = build_stream(engine)
    return stream, engine.truth.build(stream, incidents)
