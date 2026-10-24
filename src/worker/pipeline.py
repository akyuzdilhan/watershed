"""From the wire to the desk: one Match and one Spotter per match, and a sink that stores what they produce.

The same pipeline runs on a replay file (offline, or as the fallback when Service Bus is down) and on the Service Bus subscription.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from winprob.skellam import SkellamWinProb
from worker.match import Match, make_model
from worker.spotter import Spotter


class MemorySink:
    """Keeps everything in lists: tests, the offline run, and the scoring against the ground truth."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.cards: dict[str, dict] = {}
        self.audit: list[dict] = []

    def event(self, m: dict) -> None:
        self.events.append(m)

    def card(self, card: dict) -> None:
        self.cards[card["id"]] = dict(card)

    def action(self, action: dict, card: dict) -> None:
        self.audit.append(action)
        self.cards[card["id"]] = dict(card)

    def audit_entry(self, entry: dict) -> None:
        self.audit.append(entry)

    def state(self, doc: dict) -> None:
        self.states = getattr(self, "states", {})
        self.states[doc["match_id"]] = dict(doc)


class JsonFileSink(MemorySink):
    """Writes the desk of each match to `root/<match_id>/`: cards.json, state.json and audit.jsonl. The console reads these files when Cosmos DB is not configured."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = Path(root)

    def _dir(self, match_id: str) -> Path:
        d = self.root / match_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_cards(self, match_id: str) -> None:
        cards = [c for c in self.cards.values() if c["match_id"] == match_id]
        (self._dir(match_id) / "cards.json").write_text(json.dumps(cards, indent=1, ensure_ascii=False), encoding="utf-8")

    def card(self, card: dict) -> None:
        super().card(card)
        self._write_cards(card["match_id"])

    def action(self, action: dict, card: dict) -> None:
        super().action(action, card)
        self.audit_entry(action)
        self.audit.pop()  # audit_entry already appended it once
        self._write_cards(card["match_id"])

    def audit_entry(self, entry: dict) -> None:
        super().audit_entry(entry)
        with (self._dir(entry["match_id"]) / "audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def state(self, doc: dict) -> None:
        super().state(doc)
        (self._dir(doc["match_id"]) / "state.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def match_state(match: Match, spotter: Spotter, timeline: list[dict]) -> dict:
    """The live state of a match for the console: score, clock, status and the win-probability curve."""
    last = max(match.by_seq) if match.by_seq else 0
    clock = next((m for s, m in sorted(match.by_seq.items(), reverse=True) if "clock_s" in m), None)
    final = any(m["type"] == "full_time" for m in match.by_seq.values())
    teams = {side: t["name"] for side, t in match.info["teams"].items()}
    return {
        "id": f"state-{match.match_id}", "type": "match_state", "match_id": match.match_id, "teams": teams,
        "score": match.score(), "reds": match.reds(), "final": final, "last_seq": last,
        "period": clock["period"] if clock else 1, "clock_s": clock["clock_s"] if clock else 0.0, "minute_label": clock["minute_label"] if clock else "1'",
        "wp_timeline": timeline,
    }


@dataclass
class Pipeline:
    sink: object = field(default_factory=MemorySink)
    desk: object | None = None  # writes and stages each card; None keeps the Spotter's raw cards
    hold_s: float = 3.0
    threshold: float = 0.08
    matches: dict[str, Match] = field(default_factory=dict)
    spotters: dict[str, Spotter] = field(default_factory=dict)
    timelines: dict[str, list[dict]] = field(default_factory=dict)
    state_every: int = 25  # actions between two writes of the match state

    def handle(self, m: dict, now: float | None = None) -> list[dict]:
        """Processes one message off the wire; returns the cards and card actions it produced."""
        match = self.matches.setdefault(m["match_id"], Match(m["match_id"], hold_s=self.hold_s))
        return self._process(match, match.receive(m, now))

    def flush(self) -> list[dict]:
        out = []
        for match in self.matches.values():
            out += self._process(match, match.flush())
        return out

    def _process(self, match: Match, ready: list[dict]) -> list[dict]:
        out = []
        for m in ready:
            self.sink.event(m)
            spotter = self.spotters.get(match.match_id)
            if spotter is None and match.info is not None:
                spotter = self.spotters[match.match_id] = Spotter(match, make_model(match, SkellamWinProb), self.threshold)
            if spotter is None:
                continue  # nothing to evaluate before the match information
            teams = {side: t["name"] for side, t in match.info["teams"].items()}
            timeline = self.timelines.setdefault(match.match_id, [])
            if m["type"] == "action":
                p = spotter.model.predict(match.state(m, after=True))
                timeline.append({"seq": m["seq"], "event_id": m["event_id"], "clock_s": m["clock_s"], "minute_label": m["minute_label"], "home": round(p.home, 4), "draw": round(p.draw, 4), "away": round(p.away, 4)})
            produced = False
            for result in spotter.process(m):
                produced = True
                card = spotter.cards[result["event_id"]]
                if "action" in result:
                    self.sink.action(result, card)
                    if self.desk is not None:
                        self.desk.on_action(card, result, teams)
                else:
                    self.sink.card(result)
                    if self.desk is not None:
                        self.desk.write(card, teams)
                out.append(result)
            if hasattr(self.sink, "state") and (produced or m["type"] in ("period_end", "full_time", "correction") or len(timeline) % self.state_every == 0):
                self.sink.state(match_state(match, spotter, timeline))
        return out
