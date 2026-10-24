"""The worker's view of one match: every message it has received, deduplicated, put back in provider order, with the corrections applied.

The state is derived from the set of messages rather than accumulated as they arrive, so a late or duplicated message never corrupts it: the score before an action is the goals of lower sequence number that still count. A short reorder buffer holds a message until the ones before it have arrived, or until `hold_s` seconds of feed time have passed.

The derived state is kept in small indexes (goals and red cards that count, added-time boards, first final whistle), updated each time an action, a correction or a board is applied, so a query costs the number of goals or boards rather than the number of messages. An index entry is recomputed from the action as it now stands, so a late goal, a void that arrives before its goal, or a correction applied out of order give the same answer as a full scan.
"""

from dataclasses import dataclass, field

from winprob.model import MatchState, WinProb, WinProbModel

SCHEMA_VERSION = "2.0"


class SchemaError(ValueError):
    """A message the worker cannot read: wrong schema version or missing envelope."""


@dataclass
class Match:
    match_id: str
    hold_s: float = 3.0
    messages: dict[str, dict] = field(default_factory=dict)  # message_id -> message
    by_seq: dict[int, dict] = field(default_factory=dict)
    released: int = 0  # every message up to this sequence number has been processed
    pending: dict[int, dict] = field(default_factory=dict)
    duplicates: int = 0
    late: int = 0
    versions: dict[str, dict] = field(default_factory=dict)  # event id -> action as it now stands
    voided: set[str] = field(default_factory=set)
    info: dict | None = None
    goals: dict[str, tuple[int, str]] = field(default_factory=dict)  # event id -> (seq, team) of each goal that counts
    red_cards: dict[str, tuple[int, str]] = field(default_factory=dict)  # event id -> (seq, team) of each sending-off
    boards: dict[int, list[tuple[int, float]]] = field(default_factory=dict)  # period -> (seq, minutes) of each added-time board, in the order applied
    full_time_seq: int | None = None  # lowest sequence number of a final whistle

    # --- intake ---------------------------------------------------------------------------------

    def receive(self, m: dict, now: float | None = None) -> list[dict]:
        """Takes one message off the wire; returns the messages ready to process, in provider order."""
        if m.get("schema_version") != SCHEMA_VERSION or "seq" not in m or "message_id" not in m:
            raise SchemaError(f"unsupported message: schema {m.get('schema_version')!r}, id {m.get('message_id')!r}")
        if m["message_id"] in self.messages:
            self.duplicates += 1
            return self.release(now if now is not None else m["emitted_at_s"])
        self.messages[m["message_id"]] = m
        if m["seq"] <= self.released:
            self.late += 1  # it arrives after the buffer gave up on it: process it now
            return [self._apply(m)]
        self.pending[m["seq"]] = m
        return self.release(now if now is not None else m["emitted_at_s"])

    def release(self, now: float) -> list[dict]:
        out = []
        while self.pending:
            nxt = self.released + 1
            if nxt in self.pending:
                out.append(self._apply(self.pending.pop(nxt)))
                self.released = nxt
                continue
            oldest = min(self.pending)
            if now - self.pending[oldest]["emitted_at_s"] < self.hold_s:
                break  # wait a little longer for the missing message
            self.released = oldest - 1  # give up on the gap
        return out

    def flush(self) -> list[dict]:
        return self.release(float("inf"))

    def _apply(self, m: dict) -> dict:
        self.by_seq[m["seq"]] = m
        kind = m["type"]
        if kind == "match_info":
            self.info = m
        elif kind == "action":
            self.versions[m["event_id"]] = dict(m)
            self._index(m["event_id"])
        elif kind == "correction":
            target = self.versions.get(m["target_event_id"])
            if m["op"] == "void":
                self.voided.add(m["target_event_id"])
            elif target is not None:
                target.update(m["changes"])
                target["version"] = m["new_version"]
            self._index(m["target_event_id"])
        elif kind == "added_time":
            self.boards.setdefault(m["period"], []).append((m["seq"], float(m["minutes"])))
        elif kind == "full_time" and (self.full_time_seq is None or m["seq"] < self.full_time_seq):
            self.full_time_seq = m["seq"]
        return m

    def _index(self, event_id: str) -> None:
        """Recomputes what one action contributes to the derived state, from the action as it now stands."""
        self.goals.pop(event_id, None)
        self.red_cards.pop(event_id, None)
        a = self.versions.get(event_id)
        if a is None:
            return  # a correction for an action not received yet: the action applies it when it arrives
        if self.is_goal(a):
            self.goals[event_id] = (a["seq"], a["team"])
        if a["action_type"] == "foul" and a.get("card") in ("red", "second_yellow"):
            self.red_cards[event_id] = (a["seq"], a["team"])

    # --- derived state --------------------------------------------------------------------------

    def is_goal(self, a: dict) -> bool:
        return a["type"] == "action" and a["action_type"].startswith("shot") and a["shot"]["outcome"] == "goal" and a["event_id"] not in self.voided

    @staticmethod
    def _count(index: dict[str, tuple[int, str]], before_seq: int | None) -> dict[str, int]:
        out = {"home": 0, "away": 0}
        for seq, team in index.values():
            if before_seq is None or seq < before_seq:
                out[team] += 1
        return out

    def score(self, before_seq: int | None = None) -> dict[str, int]:
        return self._count(self.goals, before_seq)

    def reds(self, before_seq: int | None = None) -> dict[str, int]:
        return self._count(self.red_cards, before_seq)

    def added_time(self, period: int, before_seq: int) -> float | None:
        """Minutes on the board most recently applied for `period` among those of lower sequence number."""
        for seq, minutes in reversed(self.boards.get(period, ())):
            if seq < before_seq:
                return minutes
        return None

    def state(self, m: dict, after: bool = False) -> MatchState:
        """Win-probability state just before message `m` (or just after, `after=True`)."""
        cut = m["seq"] + 1 if after else m["seq"]
        score, reds = self.score(cut), self.reds(cut)
        final = self.full_time_seq is not None and self.full_time_seq < cut
        return MatchState(m["clock_s"] / 60, score["home"], score["away"], reds["home"], reds["away"], period=m["period"], added_time=self.added_time(m["period"], cut), final=final)

    def model_inputs(self) -> tuple[float, float]:
        teams = self.info["teams"]
        return teams["home"]["expected_goals"], teams["away"]["expected_goals"]


def swing(a: WinProb, b: WinProb) -> float:
    """How far the result probabilities moved: half the sum of the three changes (0 to 1)."""
    return 0.5 * (abs(a.home - b.home) + abs(a.draw - b.draw) + abs(a.away - b.away))


def team_win(p: WinProb, team: str) -> float:
    return p.home if team == "home" else p.away


def make_model(match: Match, factory) -> WinProbModel:
    home, away = match.model_inputs()
    return factory(home_strength=home, away_strength=away)
