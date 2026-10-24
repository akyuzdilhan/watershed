"""From the wire to the desk: one Match and one Spotter per match, and a sink that stores what they produce.

The same pipeline runs on a replay file (offline, or as the fallback when Service Bus is down) and on the Service Bus subscription.
"""

import copy
import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from winprob.skellam import SkellamWinProb
from worker.match import Match, make_model
from worker.spotter import Spotter


PRODUCER_FIELDS = ("status", "texts", "history", "retracted_on_air")  # what the console may change on a card


class MemorySink:
    """Keeps everything in lists: tests, the offline run, and the scoring against the ground truth."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.cards: dict[str, dict] = {}
        self.audit: list[dict] = []
        self.insights: dict[str, dict] = {}

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

    def load_card(self, match_id: str, card_id: str) -> dict | None:
        return self.cards.get(card_id)

    def insight(self, doc: dict) -> None:
        self.insights[doc["id"]] = copy.deepcopy(doc)

    def load_insight(self, match_id: str, insight_id: str) -> dict | None:
        return self.insights.get(insight_id)


class JsonFileSink(MemorySink):
    """Writes the desk of each match to `root/<match_id>/`: cards.json, insights.json, state.json and audit.jsonl. The console reads these files when Cosmos DB is not configured."""

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

    def insight(self, doc: dict) -> None:
        super().insight(doc)
        docs = [d for d in self.insights.values() if d["match_id"] == doc["match_id"]]
        (self._dir(doc["match_id"]) / "insights.json").write_text(json.dumps(docs, indent=1, ensure_ascii=False), encoding="utf-8")

    def load_insight(self, match_id: str, insight_id: str) -> dict | None:
        """Reads the file, where the console writes the producer's decisions."""
        path = self._dir(match_id) / "insights.json"
        if path.exists():
            for doc in json.loads(path.read_text(encoding="utf-8")):
                if doc["id"] == insight_id:
                    self.insights[insight_id] = doc
                    return doc
        return self.insights.get(insight_id)

    def load_card(self, match_id: str, card_id: str) -> dict | None:
        """Reads the file, where the console writes the producer's decisions."""
        path = self._dir(match_id) / "cards.json"
        if path.exists():
            for card in json.loads(path.read_text(encoding="utf-8")):
                if card["id"] == card_id:
                    self.cards[card_id] = card
                    return card
        return self.cards.get(card_id)


def match_state(match: Match, spotter: Spotter, timeline: list[dict]) -> dict:
    """The live state of a match for the console: score, clock, status and the win-probability curve."""
    last = match.max_seq
    clock = match.by_seq.get(match.clock_seq) if match.clock_seq is not None else None
    final = match.full_time_seq is not None
    teams = {side: t["name"] for side, t in match.info["teams"].items()}
    return {
        "id": f"state-{match.match_id}", "type": "match_state", "match_id": match.match_id, "teams": teams,
        "score": match.score(), "reds": match.reds(), "final": final, "last_seq": last,
        "period": clock["period"] if clock else 1, "clock_s": clock["clock_s"] if clock else 0.0, "minute_label": clock["minute_label"] if clock else "1'",
        "wp_timeline": timeline,
    }


class ThreadedSink:
    """Wraps a slow sink (Cosmos DB) so the pipeline does not wait for each write.

    Events are independent documents: a pool writes them concurrently. Cards, card actions, audit entries and any other write (match state, insights) go through one ordered lane, so the documents of a card are written in the order the desk produced them. A card is copied when it is handed over, since the desk keeps changing the same dict. At most `max_in_flight` writes wait at any time: beyond it the caller blocks, which keeps memory bounded. `drain()` waits for every write handed over so far and returns the errors they raised.
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

    def load_card(self, match_id: str, card_id: str) -> dict | None:
        """Reads after the writes already handed over, so a decision is never read older than our own writes."""
        self.drain()
        return self.sink.load_card(match_id, card_id) if hasattr(self.sink, "load_card") else None

    def load_insight(self, match_id: str, insight_id: str) -> dict | None:
        self.drain()
        return self.sink.load_insight(match_id, insight_id) if hasattr(self.sink, "load_insight") else None

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
    timelines: dict[str, list[dict]] = field(default_factory=dict)
    state_every: int = 25  # actions between two writes of the match state
    history: object | None = None  # a league.history.History (or any object with its read API): turns the insight engine on
    insight_cap: int = 4  # new insights per period (first half, second half, full time)
    insight_min_value: int = 40
    engines: dict[str, object] = field(default_factory=dict)
    _history_view: object | None = None

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
            timeline = self.timelines.setdefault(match.match_id, [])
            if m["type"] == "action":
                p = spotter.model.predict(match.state(m, after=True))
                timeline.append({"seq": m["seq"], "event_id": m["event_id"], "clock_s": m["clock_s"], "minute_label": m["minute_label"], "home": round(p.home, 4), "draw": round(p.draw, 4), "away": round(p.away, 4)})
            if m["type"] in ("var_check", "correction") and m.get("target_event_id") in spotter.cards:
                self._refresh(spotter.cards[m["target_event_id"]])
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
            self._insights(match, spotter, m, teams)
            if hasattr(self.sink, "state") and (produced or m["type"] in ("period_end", "full_time", "correction") or len(timeline) % self.state_every == 0):
                self.sink.state(match_state(match, spotter, timeline))
        return out

    def _insights(self, match: Match, spotter: Spotter, m: dict, teams: dict[str, str]) -> None:
        """After the Spotter: the facts this message makes true, or no longer true, against the match and the history. Not returned by `handle`, which keeps returning cards and card actions."""
        if self.history is None:
            return
        engine = self.engines.get(match.match_id)
        if engine is None:
            from worker.insight_history import HistoryView
            from worker.insights import InsightEngine

            if self._history_view is None:
                self._history_view = HistoryView(self.history)
            engine = self.engines[match.match_id] = InsightEngine(match, self._history_view, spotter, self.insight_cap, self.insight_min_value,
                                                                  load=getattr(self.sink, "load_insight", None))
        store = getattr(self.sink, "insight", None)
        write = getattr(self.desk, "write_insight", None) if self.desk is not None else None
        for doc, needs_text in engine.process(m):
            if store is not None:
                store(doc)
            if needs_text and write is not None:
                write(doc, teams)
                if store is not None:
                    store(doc)  # the texts and the status the desk set, whether or not it stored them itself

    def insights(self, match_id: str) -> list[dict]:
        """Every insight written for a match, superseded ones included."""
        engine = self.engines.get(match_id)
        return list(engine.docs.values()) if engine else []

    def _refresh(self, card: dict) -> None:
        """Before the feed changes a card, take the producer's decisions from the store: the console writes them there, not into this process."""
        load = getattr(self.sink, "load_card", None)
        stored = load(card["match_id"], card["id"]) if load else None
        if stored is not None and stored is not card:
            for key in PRODUCER_FIELDS:
                if key in stored:
                    card[key] = copy.deepcopy(stored[key])
