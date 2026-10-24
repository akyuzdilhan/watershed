"""From the wire to the desk: one Match and one Spotter per match, and a sink that stores what they produce.

The same pipeline runs on a replay file (offline, or as the fallback when Service Bus is down) and on the Service Bus subscription.
"""

import copy
import threading
from concurrent.futures import Future, ThreadPoolExecutor
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

    def audit_entry(self, entry: dict) -> None:
        self.audit.append(entry)


class ThreadedSink:
    """Wraps a slow sink (Cosmos DB) so the pipeline does not wait for each write.

    Events are independent documents: a pool writes them concurrently. Cards, card actions, audit entries and any other write (match state) go through one ordered lane, so the documents of a card are written in the order the desk produced them. A card is copied when it is handed over, since the desk keeps changing the same dict. At most `max_in_flight` writes wait at any time: beyond it the caller blocks, which keeps memory bounded. `drain()` waits for every write handed over so far and returns the errors they raised.
    """

    def __init__(self, sink, workers: int = 8, max_in_flight: int = 128) -> None:
        self.sink = sink
        self._pool = ThreadPoolExecutor(workers, thread_name_prefix="sink-event")
        self._lane = ThreadPoolExecutor(1, thread_name_prefix="sink-lane")
        self._slots = threading.BoundedSemaphore(max_in_flight)
        self._lock = threading.Lock()
        self._pending: set[Future] = set()
        self._errors: list[BaseException] = []
        self.written = 0

    def _submit(self, pool: ThreadPoolExecutor, fn, *args) -> None:
        self._slots.acquire()
        try:
            future = pool.submit(fn, *args)
        except BaseException:
            self._slots.release()
            raise
        with self._lock:
            self._pending.add(future)
        future.add_done_callback(self._done)

    def _done(self, future: Future) -> None:
        with self._lock:
            self._pending.discard(future)
            if future.cancelled():
                pass
            elif future.exception() is not None:
                self._errors.append(future.exception())
            else:
                self.written += 1
        self._slots.release()

    def event(self, m: dict) -> None:
        self._submit(self._pool, self.sink.event, m)  # messages are never changed after they are received

    def card(self, card: dict) -> None:
        self._submit(self._lane, self.sink.card, copy.deepcopy(card))

    def action(self, action: dict, card: dict) -> None:
        self._submit(self._lane, self.sink.action, copy.deepcopy(action), copy.deepcopy(card))

    def audit_entry(self, entry: dict) -> None:
        self._submit(self._lane, self.sink.audit_entry, copy.deepcopy(entry))

    def __getattr__(self, name: str):
        """Any other write method of the wrapped sink goes through the ordered lane; other attributes are read through."""
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self.sink, name)
        if not callable(attr):
            return attr

        def ordered(*args):
            self._submit(self._lane, attr, *copy.deepcopy(args))

        return ordered

    def drain(self, timeout: float | None = None) -> list[BaseException]:
        """Waits for every write handed over so far; returns (and forgets) the errors raised since the last drain."""
        with self._lock:
            pending = list(self._pending)
        for future in pending:
            try:
                future.result(timeout)
            except BaseException:  # recorded by _done
                pass
        with self._lock:
            errors, self._errors = self._errors, []
        return errors

    def close(self) -> list[BaseException]:
        errors = self.drain()
        self._pool.shutdown()
        self._lane.shutdown()
        return errors


@dataclass
class Pipeline:
    sink: object = field(default_factory=MemorySink)
    desk: object | None = None  # writes and stages each card; None keeps the Spotter's raw cards
    hold_s: float = 3.0
    threshold: float = 0.08
    matches: dict[str, Match] = field(default_factory=dict)
    spotters: dict[str, Spotter] = field(default_factory=dict)

    def handle(self, m: dict, now: float | None = None) -> list[dict]:
        """Processes one message off the wire; returns the cards and card actions it produced."""
        match = self.matches.get(m["match_id"]) or Match(m["match_id"], hold_s=self.hold_s)
        ready = match.receive(m, now)  # an unreadable message raises here, before it creates a match
        self.matches.setdefault(match.match_id, match)
        return self._process(match, ready)

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
            for result in spotter.process(m):
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
        return out
