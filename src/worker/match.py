"""The worker's view of one match: every message it has received, deduplicated, put back in provider order, with the corrections applied.

The state is derived from the set of messages rather than accumulated as they arrive, so a late or duplicated message never corrupts it: the score before an action is the goals of lower sequence number that still count. A short reorder buffer holds a message until the ones before it have arrived, or until `hold_s` seconds of feed time have passed.
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
        if m["type"] == "match_info":
            self.info = m
        elif m["type"] == "action":
            self.versions[m["event_id"]] = dict(m)
        elif m["type"] == "correction":
            target = self.versions.get(m["target_event_id"])
            if m["op"] == "void":
                self.voided.add(m["target_event_id"])
            elif target is not None:
                target.update(m["changes"])
                target["version"] = m["new_version"]
        return m

    # --- derived state --------------------------------------------------------------------------

    def is_goal(self, a: dict) -> bool:
        return a["type"] == "action" and a["action_type"].startswith("shot") and a["shot"]["outcome"] == "goal" and a["event_id"] not in self.voided

    def score(self, before_seq: int | None = None) -> dict[str, int]:
        score = {"home": 0, "away": 0}
        for a in self.versions.values():
            if (before_seq is None or a["seq"] < before_seq) and self.is_goal(a):
                score[a["team"]] += 1
        return score

    def reds(self, before_seq: int | None = None) -> dict[str, int]:
        reds = {"home": 0, "away": 0}
        for a in self.versions.values():
            if (before_seq is None or a["seq"] < before_seq) and a["action_type"] == "foul" and a.get("card") in ("red", "second_yellow"):
                reds[a["team"]] += 1
        return reds

    def added_time(self, period: int, before_seq: int) -> float | None:
        board = [m for s, m in self.by_seq.items() if s < before_seq and m["type"] == "added_time" and m["period"] == period]
        return float(board[-1]["minutes"]) if board else None

    def state(self, m: dict, after: bool = False) -> MatchState:
        """Win-probability state just before message `m` (or just after, `after=True`)."""
        cut = m["seq"] + 1 if after else m["seq"]
        score, reds = self.score(cut), self.reds(cut)
        final = any(x["type"] == "full_time" for s, x in self.by_seq.items() if s < cut)
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
