"""From the wire to the desk: one Match and one Spotter per match, and a sink that stores what they produce.

The same pipeline runs on a replay file (offline, or as the fallback when Service Bus is down) and on the Service Bus subscription.
"""

from dataclasses import dataclass, field

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


@dataclass
class Pipeline:
    sink: object = field(default_factory=MemorySink)
    hold_s: float = 3.0
    threshold: float = 0.08
    matches: dict[str, Match] = field(default_factory=dict)
    spotters: dict[str, Spotter] = field(default_factory=dict)

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
            for result in spotter.process(m):
                if "action" in result:
                    self.sink.action(result, spotter.cards[result["event_id"]])
                else:
                    self.sink.card(result)
                out.append(result)
        return out
