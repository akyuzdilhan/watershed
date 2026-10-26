"""From the wire to the desk: one Match and one Spotter per match, and a sink that stores what they produce.

The same pipeline runs on a replay file (offline, or as the fallback when Service Bus is down) and on the Service Bus subscription. With a desk, the desk writes every card, audits every card action once (the Spotter's action goes to the audit trail only when there is no desk), and at the final whistle writes the post-match recap from the cards as the store has them. Each draft and each card action of the desk runs in a span (`worker.telemetry`), recorded only when traces are configured.
"""

import copy
import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from winprob.skellam import SkellamWinProb
from worker import telemetry
from worker.match import Match, make_model
from worker.spotter import Spotter

log = logging.getLogger("worker")

PRODUCER_FIELDS = ("status", "texts", "history", "retracted_on_air")  # what the console may change on a card
INSIGHT_PRODUCER_FIELDS = ("status", "texts", "history")  # what the console may change on an insight


class MemorySink:
    """Keeps everything in lists: tests, the offline run, and the scoring against the ground truth."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.cards: dict[str, dict] = {}
        self.audit: list[dict] = []
        self.insights: dict[str, dict] = {}
        self.recaps: dict[str, dict] = {}  # match id -> post-match recap

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

    def recap(self, doc: dict) -> None:
        self.recaps[doc["match_id"]] = copy.deepcopy(doc)


class JsonFileSink(MemorySink):
    """Writes the desk of each match to `root/<match_id>/`: cards.json, insights.json, state.json, audit.jsonl and, at the final whistle, recap.json. The console reads these files when Cosmos DB is not configured, and writes the producer's decisions into cards.json and insights.json: before rewriting one of them, the sink takes those decisions back from it for every document other than the one it writes, so a later card does not undo an approval."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = Path(root)

    def _dir(self, match_id: str) -> Path:
        d = self.root / match_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _keep_decisions(path: Path, docs: dict[str, dict], written_id: str, fields: tuple[str, ...]) -> None:
        """The console's decisions in the file at `path` (`fields` of every document but `written_id`, at the version this sink last wrote) back into `docs`."""
        try:
            stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except (OSError, ValueError):  # unreadable while the console replaces it: written as this process has it
            return
        for doc in stored if isinstance(stored, list) else []:
            mine = docs.get(doc.get("id")) if isinstance(doc, dict) else None
            if mine is None or doc["id"] == written_id or doc.get("version") != mine.get("version"):
                continue
            for k in fields:
                if k in doc:
                    mine[k] = doc[k]

    def _write_cards(self, match_id: str, written_id: str) -> None:
        path = self._dir(match_id) / "cards.json"
        self._keep_decisions(path, self.cards, written_id, PRODUCER_FIELDS)
        cards = [c for c in self.cards.values() if c["match_id"] == match_id]
        path.write_text(json.dumps(cards, indent=1, ensure_ascii=False), encoding="utf-8")

    def card(self, card: dict) -> None:
        super().card(card)
        self._write_cards(card["match_id"], card["id"])

    def action(self, action: dict, card: dict) -> None:
        super().action(action, card)
        self.audit_entry(action)
        self.audit.pop()  # audit_entry already appended it once
        self._write_cards(card["match_id"], card["id"])

    def audit_entry(self, entry: dict) -> None:
        super().audit_entry(entry)
        with (self._dir(entry["match_id"]) / "audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def state(self, doc: dict) -> None:
        super().state(doc)
        (self._dir(doc["match_id"]) / "state.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def insight(self, doc: dict) -> None:
        """Insights go to insights.json; the storyline, which goes through the same desk path, to storyline.json."""
        super().insight(doc)
        if doc.get("type") == "storyline":
            (self._dir(doc["match_id"]) / "storyline.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
            return
        path = self._dir(doc["match_id"]) / "insights.json"
        self._keep_decisions(path, self.insights, doc["id"], INSIGHT_PRODUCER_FIELDS)
        docs = [d for d in self.insights.values() if d["match_id"] == doc["match_id"] and d.get("type") != "storyline"]
        path.write_text(json.dumps(docs, indent=1, ensure_ascii=False), encoding="utf-8")

    def recap(self, doc: dict) -> None:
        """recap.json, as `python -m worker.recap` writes it."""
        super().recap(doc)
        (self._dir(doc["match_id"]) / "recap.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")

    def feedback(self, doc: dict) -> None:
        """A producer's judgement on one sentence: appended to feedback.jsonl, never into cards.json."""
        self.feedbacks = getattr(self, "feedbacks", [])
        self.feedbacks.append(dict(doc))
        with (self._dir(doc["match_id"]) / "feedback.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")

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
    storyline_for: object | None = None  # callable(match_id, teams) -> storyline document, built once when the match information arrives
    storylines: dict[str, dict] = field(default_factory=dict)
    recap: bool = True  # with a desk: the post-match recap once the final whistle is processed, written again if the feed changes a card after it, or if a flush finds a producer decision made since
    recaps: dict[str, dict] = field(default_factory=dict)
    _recapped: dict[str, tuple] = field(default_factory=dict)  # per match: (card id, status, version) of each card the last recap was written from
    recap_window_s: float = 3600.0  # how long after its final whistle a flush still reads a match's cards back for the producer's decisions
    clock: object = time.monotonic  # seconds, for recap_window_s
    _whistled: dict[str, float] = field(default_factory=dict)  # match id -> clock when its full_time message was processed (Match.full_time_seq is set as soon as it is released)
    _tables: dict[str, list] = field(default_factory=dict)  # per match: (event id, table if the score holds) of each goal given a season context

    def handle(self, m: dict, now: float | None = None) -> list[dict]:
        """Processes one message off the wire; returns the cards and card actions it produced."""
        match = self.matches.get(m["match_id"]) or Match(m["match_id"], hold_s=self.hold_s)
        ready = match.receive(m, now)  # an unreadable message raises here, before it creates a match
        self.matches.setdefault(match.match_id, match)
        return self._process(match, ready)

    def flush(self) -> list[dict]:
        """Processes what the reorder buffers still hold. A match finished less than `recap_window_s` ago gets its recap written again when a producer decided on one of its cards since the last one (the listener flushes when a session goes quiet); older ones are not read back."""
        out = []
        for match in self.matches.values():
            out += self._process(match, match.flush())
            whistled = self._whistled.get(match.match_id)
            if self.recap and whistled is not None and self.clock() - whistled <= self.recap_window_s and match.match_id in self.spotters:
                self._recap(match, self.spotters[match.match_id], self.timelines.get(match.match_id, []), only_if_changed=True)
        return out

    def _process(self, match: Match, ready: list[dict]) -> list[dict]:
        out = []
        for m in ready:
            self.sink.event(m)
            whistle = m["type"] == "full_time" and match.match_id not in self._whistled
            if whistle:
                self._whistled[match.match_id] = self.clock()
            spotter = self.spotters.get(match.match_id)
            if spotter is None and match.info is not None:
                spotter = self.spotters[match.match_id] = Spotter(match, make_model(match, SkellamWinProb), self.threshold)
                self._storyline(match)
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
                    if self.desk is not None:
                        with telemetry.span("desk.action", match_id=match.match_id, card_id=card["id"], kind=card["moment"], action=result["action"], cause=result.get("cause")) as s:
                            self.desk.on_action(card, result, teams)  # audits the action once, with its own fields, and stores the card
                            telemetry.annotate(s, **(telemetry.outcome(card) if result["action"] == "rewrite" else {"status": card.get("status"), "version": card.get("version")}))
                    else:
                        self.sink.action(result, card)
                else:
                    self._season(match, card)
                    self.sink.card(result)
                    if self.desk is not None:
                        with telemetry.span("desk.write", match_id=match.match_id, card_id=card["id"], kind=card["moment"]) as s:
                            self.desk.write(card, teams)
                            telemetry.annotate(s, **telemetry.outcome(card))
                out.append(result)
            self._insights(match, spotter, m, teams)
            if hasattr(self.sink, "state") and (produced or m["type"] in ("period_end", "full_time", "correction") or len(timeline) % self.state_every == 0):
                self.sink.state(match_state(match, spotter, timeline))
            if self.recap and match.match_id in self._whistled and (whistle or produced):  # not for the messages released with the whistle but before it
                self._recap(match, spotter, timeline)
        return out

    def _recap(self, match: Match, spotter: Spotter, timeline: list[dict], only_if_changed: bool = False) -> None:
        """The post-match recap, by the desk, from the cards with the producer's decisions read back from the store: at the final whistle, after a card action that comes later, and on a flush when a card's status or version changed since the last recap (`only_if_changed`)."""
        write = getattr(self.desk, "write_recap", None)
        if write is None:
            return
        cards = list(spotter.cards.values())
        try:
            for card in cards:
                self._refresh(card)
            seen = tuple(sorted((c["id"], c.get("status"), c.get("version", 1)) for c in cards))
            if only_if_changed and self._recapped.get(match.match_id) == seen:
                return
            with telemetry.span("desk.write_recap", match_id=match.match_id, card_id=f"recap-{match.match_id}", kind="recap", cards=len(cards)) as s:
                self.recaps[match.match_id] = write(cards, match_state(match, spotter, timeline), self.storylines.get(match.match_id))
                telemetry.annotate(s, **telemetry.outcome(self.recaps[match.match_id]))
            self._recapped[match.match_id] = seen
        except Exception as error:  # the final whistle is processed, and the flush goes on, even if the recap fails
            log.warning("recap of %s failed: %s: %s", match.match_id, type(error).__name__, str(error)[:300])

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
                                                                  load=getattr(self.sink, "load_insight", None), audit=getattr(self.sink, "audit_entry", None))
        store = getattr(self.sink, "insight", None)
        write = getattr(self.desk, "write_insight", None) if self.desk is not None else None
        for doc, needs_text in engine.process(m):
            if store is not None:
                store(doc)
            if needs_text and write is not None:
                self._write_insight(write, doc, teams)
                if store is not None:
                    store(doc)  # the texts and the status the desk set, whether or not it stored them itself

    def _season(self, match: Match, card: dict) -> None:
        """What the score after a goal (or at a red card) means for the season, from the pre-match storyline: on the card, for the Writer."""
        story = self.storylines.get(match.match_id)
        if story is None or card["moment"] not in ("goal", "red_card"):
            return
        from worker.storyline import live_context

        score = dict(card["score_before"])
        if card["moment"] == "goal":
            score[card["team"]] += 1
        facts, meanings = live_context(story, {"score": score})
        table = tuple(sorted((k, v) for k, v in facts.items() if k.endswith(("_rank_if_score_holds", "_if_score_holds")) and not k.endswith("_pct")))
        said = [(eid, t) for eid, t in self._tables.get(match.match_id, []) if eid not in match.voided]
        if said and said[-1][1] == table:
            return  # the table would read the same as after the last goal that counts: nothing new to say
        if said:  # entering or leaving the zone is told against the score before this goal, not against kick-off
            prev = dict(said[-1][1])
            for k in [k for k in facts if k.endswith(("_leaves_drop_zone_if_score_holds", "_enters_drop_zone_if_score_holds"))]:
                facts.pop(k)
                meanings.pop(k, None)
            for k, now in list(facts.items()):
                if k.endswith("_in_drop_zone_if_score_holds") and k in prev and prev[k] != now:
                    club = k[: -len("_in_drop_zone_if_score_holds")]
                    flag = f"{club}_{'enters' if now else 'leaves'}_drop_zone_if_score_holds"
                    facts[flag] = True
                    meanings[flag] = f"{club} {'drop into' if now else 'climb out of'} the drop zone with this goal, if the score holds"
        self._tables.setdefault(match.match_id, []).append((card["event_id"], table))
        card["season_context"] = {"facts": facts, "meanings": meanings}

    def _storyline(self, match: Match) -> None:
        """Before kick-off: the context of the match, written and checked by the desk like an insight, stored next to the cards."""
        if self.storyline_for is None:
            return
        teams = {side: t["name"] for side, t in match.info["teams"].items()}
        doc = self.storyline_for(match.match_id, teams)
        if doc is None:
            return
        self.storylines[match.match_id] = doc
        store = getattr(self.sink, "insight", None)
        write = getattr(self.desk, "write_insight", None) if self.desk is not None else None
        if write is not None:
            self._write_insight(write, doc, teams)
        if store is not None:
            store(doc)

    @staticmethod
    def _write_insight(write, doc: dict, teams: dict[str, str]) -> None:
        """The desk's draft of an insight or of the storyline, in a span."""
        with telemetry.span("desk.write_insight", match_id=doc.get("match_id"), card_id=doc.get("id"), kind=doc.get("kind") or doc.get("type")) as s:
            write(doc, teams)
            telemetry.annotate(s, **telemetry.outcome(doc))

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
