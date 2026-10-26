"""The worker's input and output without Azure: concurrent writes, card ordering, incremental match state, the Service Bus loop on fakes, and the post-match recap."""

import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from generator import generate_match
from worker import azure_io
from worker.azure_io import ErrorLog, NoSession, consume, with_retry
from worker.desk import Desk
from worker.match import Match
from worker.pipeline import JsonFileSink, MemorySink, Pipeline, ThreadedSink
from worker.writer import TemplateWriter


SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(scope="module")
def demo():
    return generate_match(7, "demo")


# --- fakes ------------------------------------------------------------------------------------------


class SlowSink(MemorySink):
    """A MemorySink whose every write takes `delay_s`, like a round trip to Cosmos DB; it records the write order and the peak concurrency."""

    def __init__(self, delay_s: float = 0.02, jitter_s: float = 0.0, fail_events: bool = False) -> None:
        super().__init__()
        self.delay_s, self.jitter_s, self.fail_events = delay_s, jitter_s, fail_events
        self.order: list[tuple] = []
        self.active = self.peak = 0
        self._lock = threading.Lock()
        self._rng = random.Random(3)

    def _wait(self) -> None:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            delay = self.delay_s + self.jitter_s * self._rng.random()
        time.sleep(delay)
        with self._lock:
            self.active -= 1

    def event(self, m: dict) -> None:
        self._wait()
        if self.fail_events:
            raise ConnectionError("cosmos is down")
        with self._lock:
            super().event(m)

    def card(self, card: dict) -> None:
        self._wait()
        self.order.append(("card", card["id"], card["status"], card["version"]))
        super().card(card)

    def action(self, action: dict, card: dict) -> None:
        self._wait()
        self.order.append(("action", action["id"]))
        self.order.append(("card", card["id"], card["status"], card["version"]))
        super().action(action, card)

    def audit_entry(self, entry: dict) -> None:
        self._wait()
        self.order.append(("audit", entry["id"]))
        super().audit_entry(entry)


class RecordingSink(SlowSink):
    def __init__(self) -> None:
        super().__init__(delay_s=0.0)


class FakeMessage:
    def __init__(self, body: str) -> None:
        self.body = body

    def __str__(self) -> str:
        return self.body


class FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class FakeReceiver:
    """One session: hands out its messages in batches, then reports idle by returning an empty batch after `max_wait_time` of fake time."""

    def __init__(self, session_id: str, bodies: list[str], clock: "FakeClock", log: dict) -> None:
        self.session = FakeSession(session_id)
        self.queue = [FakeMessage(b) for b in bodies]
        self.clock, self.log = clock, log

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.log.setdefault("closed", []).append(self.session.session_id)

    def receive_messages(self, max_message_count: int, max_wait_time: float) -> list:
        if not self.queue:
            self.clock.now += max_wait_time
            return []
        batch, self.queue = self.queue[:max_message_count], self.queue[max_message_count:]
        self.clock.now += 0.01
        return batch

    def complete_message(self, msg) -> None:
        self.log.setdefault("completed", []).append(msg)

    def abandon_message(self, msg) -> None:
        self.log.setdefault("abandoned", []).append(msg)

    def dead_letter_message(self, msg, reason: str, error_description: str) -> None:
        self.log.setdefault("dead_lettered", []).append((msg, reason, error_description))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def fake_bus(sessions: dict[str, list[str]], clock: FakeClock, log: dict):
    """open_session: the next session with messages, or NoSession after waiting `wait_s` of fake time."""
    waiting = list(sessions.items())

    def open_session(wait_s: float):
        log.setdefault("waits", []).append(wait_s)
        if not waiting:
            clock.now += wait_s
            raise NoSession()
        session_id, bodies = waiting.pop(0)
        return FakeReceiver(session_id, bodies, clock, log)

    return open_session


def offline(stream, sink=None, desk=None):
    pipeline = Pipeline(sink=sink or MemorySink(), desk=desk)
    results = [r for m in stream for r in pipeline.handle(m)] + pipeline.flush()
    return pipeline, results


def key(results):
    return sorted((r.get("action", "card"), r["event_id"]) for r in results)


# --- 1. throughput -----------------------------------------------------------------------------------


def test_events_are_written_concurrently(demo):
    stream, _ = demo
    stream = stream[:400]
    slow = SlowSink(delay_s=0.02)
    sink = ThreadedSink(slow, workers=16)
    start = time.perf_counter()
    offline(stream, sink=sink)
    assert sink.close() == []
    elapsed = time.perf_counter() - start
    sequential = 0.02 * len(slow.events)
    assert len(slow.events) == len({m["message_id"] for m in stream})
    assert 8 <= slow.peak <= 16 + 1  # the pool, plus the lane for cards
    assert elapsed < sequential / 4, (elapsed, sequential)


def test_writes_in_flight_are_bounded():
    slow = SlowSink(delay_s=0.005)
    sink = ThreadedSink(slow, workers=4, max_in_flight=6)
    peak = 0
    for i in range(60):
        sink.event({"message_id": f"m{i}"})
        peak = max(peak, len(sink._pending))
    sink.close()
    assert peak <= 6 and len(slow.events) == 60


def test_card_writes_keep_the_desk_order(demo):
    """Through the threaded sink, cards and audit entries reach the store in the order the desk produced them, with the card as it was then."""
    stream, _ = demo
    direct = RecordingSink()
    offline(stream, sink=direct, desk=Desk(direct, TemplateWriter(), auto_approve=True))
    slow = SlowSink(delay_s=0.001, jitter_s=0.004)
    threaded = ThreadedSink(slow, workers=8)
    offline(stream, sink=threaded, desk=Desk(threaded, TemplateWriter(), auto_approve=True))
    assert threaded.close() == []
    assert slow.order == direct.order and len(slow.order) > 40
    assert slow.cards == direct.cards
    assert [c["status"] for c in slow.cards.values()].count("retracted") == 2


def test_other_writes_go_through_the_ordered_lane():
    class StateSink(MemorySink):
        def __init__(self):
            super().__init__()
            self.states = []

        def match_state(self, state):
            self.states.append(state)

    inner = StateSink()
    sink = ThreadedSink(inner)
    state = {"score": {"home": 0}}
    sink.match_state(state)
    state["score"]["home"] = 1  # changed after the hand-over: the copy is written
    sink.match_state(state)
    sink.close()
    assert [s["score"]["home"] for s in inner.states] == [0, 1]
    assert sink.states is inner.states  # attributes read through


def test_failed_writes_are_returned_by_drain():
    sink = ThreadedSink(SlowSink(delay_s=0.0, fail_events=True), workers=2)
    for i in range(5):
        sink.event({"message_id": f"m{i}"})
    errors = sink.drain()
    assert len(errors) == 5 and all(isinstance(e, ConnectionError) for e in errors)
    assert sink.close() == []


# --- 2. incremental match state ----------------------------------------------------------------------


def scan_score(match: Match, before_seq=None):
    score = {"home": 0, "away": 0}
    for a in match.versions.values():
        if (before_seq is None or a["seq"] < before_seq) and match.is_goal(a):
            score[a["team"]] += 1
    return score


def scan_reds(match: Match, before_seq=None):
    reds = {"home": 0, "away": 0}
    for a in match.versions.values():
        if (before_seq is None or a["seq"] < before_seq) and a["action_type"] == "foul" and a.get("card") in ("red", "second_yellow"):
            reds[a["team"]] += 1
    return reds


def scan_added_time(match: Match, period, before_seq):
    board = [m for s, m in match.by_seq.items() if s < before_seq and m["type"] == "added_time" and m["period"] == period]
    return float(board[-1]["minutes"]) if board else None


def scan_final(match: Match, cut):
    return any(x["type"] == "full_time" for s, x in match.by_seq.items() if s < cut)


def assert_same_state(match: Match, cuts):
    assert match.score() == scan_score(match) and match.reds() == scan_reds(match)
    for cut in cuts:
        assert match.score(cut) == scan_score(match, cut), cut
        assert match.reds(cut) == scan_reds(match, cut), cut
        for period in (1, 2):
            assert match.added_time(period, cut) == scan_added_time(match, period, cut), cut
        assert (match.full_time_seq is not None and match.full_time_seq < cut) == scan_final(match, cut)


def disorder(stream, rng):
    """Neighbour swaps, goals and corrections sent far too late (past the reorder buffer), and duplicates."""
    out = list(stream)
    for i in range(0, len(out) - 4, 3):
        if rng.random() < 0.3:
            out[i], out[i + 2] = out[i + 2], out[i]
    movers = [m for m in out if m["type"] == "correction" or (m["type"] == "action" and m["action_type"].startswith("shot") and m["shot"]["outcome"] == "goal")]
    for m in rng.sample(movers, min(len(movers), 6)):
        i = out.index(m)
        out.insert(min(len(out), i + rng.randint(80, 300)), out.pop(i))
    out += rng.sample(stream, 40)
    return out


def extra_corrections(stream, rng):
    """Voids and outcome changes the unscripted feed rarely sends: goals voided or turned into saves, a shot turned into a goal, a red card voided."""
    shots = [m for m in stream if m["type"] == "action" and m["action_type"].startswith("shot")]
    goals = [m for m in shots if m["shot"]["outcome"] == "goal"]
    reds = [m for m in stream if m["type"] == "action" and m["action_type"] == "foul" and m.get("card") in ("red", "second_yellow")]
    seq = max(m["seq"] for m in stream)
    out = []

    def correction(target, op, changes):
        nonlocal seq
        seq += 1
        return {"schema_version": "2.0", "match_id": target["match_id"], "type": "correction", "message_id": f"extra-{seq}", "seq": seq, "op": op, "target_event_id": target["event_id"],
                "changes": changes, "new_version": 2, "cause": "test", "clock_s": target["clock_s"], "period": target["period"], "emitted_at_s": target["emitted_at_s"]}

    for g in rng.sample(goals, min(2, len(goals))):
        out.append(correction(g, "void", {}))
    for g in rng.sample(goals, min(1, len(goals))):
        out.append(correction(g, "update", {"shot": {**g["shot"], "outcome": "saved"}}))
    for s in rng.sample(shots, 2):
        out.append(correction(s, "update", {"shot": {**s["shot"], "outcome": "goal"}}))
    for r in reds[:1]:
        out.append(correction(r, "void", {}))
    return out


def inject(stream, extra, rng):
    """Spreads the extra corrections over the second half, some before the action they correct."""
    out = list(stream)
    for c in extra:
        out.insert(rng.randint(len(out) // 2, len(out)), c)
    return out


@pytest.mark.parametrize("seed", range(1, 21))
def test_incremental_state_equals_a_full_scan(seed):
    stream, truth = generate_match(seed, frames=False)
    rng = random.Random(seed)
    extra = extra_corrections(stream, rng)
    shuffled = inject(disorder(stream, rng), extra, rng)
    match = Match("m")
    top = max(m["seq"] for m in shuffled) + 2
    for n, m in enumerate(shuffled):
        match.receive(m)
        if n % 60 == 0:
            assert_same_state(match, [rng.randint(1, top) for _ in range(6)])
    match.flush()
    assert_same_state(match, range(1, top, 7))
    assert match.late > 0 and match.duplicates >= 40


def test_incremental_state_on_the_demo(demo):
    stream, truth = demo
    rng = random.Random(0)
    match = Match("m")
    for m in disorder(stream, rng):
        match.receive(m)
    match.flush()
    assert_same_state(match, range(1, max(m["seq"] for m in stream) + 2, 3))
    assert match.score() == truth["final_score"] and len(match.voided) == 2


def test_late_goal_and_void_before_its_goal():
    base = {"schema_version": "2.0", "match_id": "m", "clock_s": 60, "period": 1}
    goal = lambda seq, eid, team: {**base, "type": "action", "message_id": f"x{seq}", "seq": seq, "event_id": eid, "team": team, "action_type": "shot", "shot": {"outcome": "goal", "xg": 0.3}, "emitted_at_s": seq}
    void = {**base, "type": "correction", "message_id": "x9", "seq": 9, "op": "void", "target_event_id": "g5", "changes": {}, "emitted_at_s": 9}
    match = Match("m", hold_s=1.0)
    match.receive(goal(2, "g2", "home"))
    match.receive(void, now=9)  # gives up on 3..8: the void is applied before its goal
    assert match.score() == {"home": 1, "away": 0}
    match.receive(goal(5, "g5", "away"), now=20)  # the voided goal, late
    match.receive(goal(4, "g4", "away"), now=21)  # a late goal that counts
    assert match.score() == {"home": 1, "away": 1} == scan_score(match)
    assert match.score(5) == {"home": 1, "away": 1} and match.score(4) == {"home": 1, "away": 0}
    reassign = {**base, "type": "correction", "message_id": "x10", "seq": 10, "op": "update", "target_event_id": "g4", "changes": {"shot": {"outcome": "saved", "xg": 0.3}}, "new_version": 2, "emitted_at_s": 10}
    match.receive(reassign, now=22)
    assert match.score() == {"home": 1, "away": 0} == scan_score(match)


def test_offline_results_are_unchanged(demo):
    stream, truth = demo
    _, results = offline(stream)
    assert len(results) == 18 and sum("action" not in r for r in results) == 11


# --- 3. the Service Bus loop on fakes ----------------------------------------------------------------


def bodies(stream):
    return [json.dumps(m) for m in stream]


def test_listener_stops_about_idle_s_after_the_last_message(demo):
    stream, _ = demo
    clock, log = FakeClock(), {}
    pipeline = Pipeline()
    stats = consume(pipeline, fake_bus({"demo": bodies(stream)}, clock, log), idle_s=30, clock=clock)
    assert stats["completed"] == len(stream) and not log.get("abandoned")
    assert len(log["completed"]) == len(stream)
    # last batch at t0; the session reports idle 30 s later, one 2 s look for another session, then stop
    assert log["waits"] == [30, azure_io.MIN_WAIT_S]
    _, expected = offline(stream)
    assert sorted(c["id"] for c in pipeline.sink.cards.values()) == sorted(r["id"] for r in expected if "action" not in r)


def test_listener_takes_every_waiting_session_before_stopping():
    a, b = generate_match(1, frames=False)[0], generate_match(2, frames=False)[0]
    clock, log = FakeClock(), {}
    pipeline = Pipeline()
    stats = consume(pipeline, fake_bus({"a": bodies(a), "b": bodies(b)}, clock, log), idle_s=30, clock=clock)
    assert stats["completed"] == len(a) + len(b)
    assert log["closed"] == ["a", "b"] and len(pipeline.matches) == 2


def test_forever_keeps_waiting_until_interrupted():
    clock, log = FakeClock(), {}
    calls = []

    def open_session(wait_s):
        calls.append(wait_s)
        if len(calls) == 5:
            raise KeyboardInterrupt
        clock.now += wait_s
        raise NoSession()

    stats = consume(Pipeline(), open_session, idle_s=30, forever=True, clock=clock)
    assert calls == [30] * 5 and stats["interrupted"] == 1


def test_poison_messages_are_dead_lettered_and_logged_once(demo, caplog):
    stream, _ = demo
    good = bodies(stream[:300])
    poison = ["not json", "{also not json", json.dumps({"schema_version": "0.1.0", "message_id": "x", "seq": 1, "match_id": "m"}), "[1, 2]", "not json either"]
    mixed = good[:50] + poison[:2] + good[50:200] + poison[2:] + good[200:]
    clock, log = FakeClock(), {}
    pipeline = Pipeline()
    with caplog.at_level(logging.WARNING, logger="worker"):
        stats = consume(pipeline, fake_bus({"demo": mixed}, clock, log), idle_s=30, clock=clock)
    assert stats["dead_lettered"] == 5 and stats["completed"] == 300
    assert [str(m) for m, _, _ in log["dead_lettered"]] == poison
    assert all(reason == "unreadable" for _, reason, _ in log["dead_lettered"])
    first_lines = [r for r in caplog.records if "dead-lettered" in r.getMessage() and "further" in r.getMessage()]
    assert len(first_lines) == 3  # one per kind of error: bad JSON, unknown schema, not an object
    _, expected = offline(stream[:300])
    assert key(r for r in pipeline.sink.audit) == key(r for r in expected if "action" in r)


def test_interrupt_completes_what_is_stored_and_abandons_the_rest(demo):
    stream, _ = demo
    clock, log = FakeClock(), {}
    pipeline = Pipeline()
    handle, seen = pipeline.handle, []

    def interrupted(m, now=None):
        seen.append(m)
        if len(seen) == 130:
            raise KeyboardInterrupt
        return handle(m, now)

    pipeline.handle = interrupted
    stats = consume(pipeline, fake_bus({"demo": bodies(stream[:300])}, clock, log), idle_s=30, batch_size=100, clock=clock)
    assert stats["interrupted"] == 1
    assert len(log["completed"]) == 129 and len(log["abandoned"]) == 100 - 29
    assert log["closed"] == ["demo"]


def test_store_errors_are_logged_once_and_do_not_stop_the_loop(demo, caplog):
    stream, _ = demo
    clock, log = FakeClock(), {}
    pipeline = Pipeline(sink=ThreadedSink(SlowSink(delay_s=0.0, fail_events=True), workers=4))
    with caplog.at_level(logging.WARNING, logger="worker"):
        stats = consume(pipeline, fake_bus({"demo": bodies(stream[:250])}, clock, log), idle_s=30, clock=clock)
    assert stats["completed"] == 250
    store = [r.getMessage() for r in caplog.records if r.getMessage().startswith("store")]
    assert len(store) == 2 and " x ConnectionError" in store[1]  # the first error, then the summary


def test_session_errors_are_retried():
    clock, log, calls = FakeClock(), {}, []

    def open_session(wait_s):
        calls.append(wait_s)
        if len(calls) <= 2:
            raise RuntimeError("amqp link detached")
        clock.now += wait_s
        raise NoSession()

    slept = []
    errors = ErrorLog()
    consume(Pipeline(), open_session, idle_s=30, clock=clock, sleep=slept.append, errors=errors)
    assert len(slept) == 2 and errors.counts[("service bus", "RuntimeError")] == 2


# --- 4. retries --------------------------------------------------------------------------------------


class HttpError(Exception):
    def __init__(self, status_code, headers=None):
        super().__init__(f"status {status_code}")
        self.status_code, self.headers = status_code, headers or {}


def test_transient_store_errors_are_retried_with_backoff():
    attempts, slept = [], []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise HttpError(429, {"x-ms-retry-after-ms": "250"})
        if len(attempts) == 2:
            raise HttpError(503)
        return "ok"

    assert with_retry(flaky, sleep=slept.append) == "ok"
    assert len(attempts) == 3 and 0.25 <= slept[0] <= 0.3 and 1.0 <= slept[1] <= 1.2


def test_permanent_store_errors_are_not_retried():
    attempts = []

    def bad():
        attempts.append(1)
        raise HttpError(400)

    with pytest.raises(HttpError):
        with_retry(bad, sleep=lambda s: None)
    assert len(attempts) == 1


def test_retries_give_up_after_the_last_attempt():
    attempts = []

    def down():
        attempts.append(1)
        raise HttpError(503)

    with pytest.raises(HttpError):
        with_retry(down, attempts=4, sleep=lambda s: None)
    assert len(attempts) == 4


def test_match_state_document_matches_a_full_scan(demo):
    stream, truth = demo
    pipeline, _ = offline(disorder(stream, random.Random(4)))
    match = next(iter(pipeline.matches.values()))
    doc = pipeline.sink.states[match.match_id]
    clock = next(m for s, m in sorted(match.by_seq.items(), reverse=True) if "clock_s" in m)
    assert doc["last_seq"] == max(match.by_seq) and doc["clock_s"] == clock["clock_s"] and doc["minute_label"] == clock["minute_label"]
    assert doc["final"] and doc["score"] == truth["final_score"] == scan_score(match)
    assert len(doc["wp_timeline"]) == sum(m["type"] == "action" for m in match.by_seq.values())


def test_a_failed_completion_releases_the_session_and_the_loop_goes_on(demo):
    stream, _ = demo
    clock, log = FakeClock(), {}
    sessions = {"lost": bodies(stream[:150]), "next": bodies(generate_match(3, frames=False)[0][:50])}
    bus = fake_bus(sessions, clock, log)

    def open_session(wait_s):
        receiver = bus(wait_s)
        if receiver.session.session_id == "lost":
            def lock_lost(msg):
                raise RuntimeError("session lock lost")
            receiver.complete_message = lock_lost
        return receiver

    errors = ErrorLog()
    stats = consume(Pipeline(), open_session, idle_s=30, clock=clock, errors=errors)
    assert errors.counts[("session", "RuntimeError")] == 1
    assert log["closed"] == ["lost", "next"] and stats["completed"] == 50


def test_producer_decisions_survive_feed_updates(tmp_path):
    """The console approves a card in the store; the worker's later retraction must see it was on air."""
    import json as _json

    from generator import generate_match
    from worker.desk import Desk
    from worker.pipeline import JsonFileSink, Pipeline
    from worker.writer import TemplateWriter

    stream, truth = generate_match(7, "demo", frames=False)
    sink = JsonFileSink(tmp_path)
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter()))
    goal = next(m["event_id"] for m in truth["moments"] if m["key"] == "goal-63")
    path = tmp_path / "demo-0007" / "cards.json"
    approved = False
    for m in sorted(stream, key=lambda m: m["emitted_at_s"]):
        pipeline.handle(m)
        if not approved and path.exists():
            cards = _json.loads(path.read_text(encoding="utf-8"))
            for c in cards:
                if c["event_id"] == goal:  # what the console does: approve in the store, not in the worker
                    c["status"] = "approved"
                    c.setdefault("history", []).append({"action": "approved", "by": "producer", "version": c["version"]})
                    path.write_text(_json.dumps(cards), encoding="utf-8")
                    approved = True
    pipeline.flush()
    card = sink.cards[f"card-{goal}"]
    assert approved and card["status"] == "retracted" and card["retracted_on_air"]
    assert any(h["by"] == "producer" for h in card["history"])


def test_thin_timeline_keeps_jumps_and_the_end():
    from worker.azure_io import thin_timeline

    points = [{"home": 0.3, "draw": 0.3, "away": 0.4} for _ in range(100)]
    points[50] = points[51] = {"home": 0.7, "draw": 0.2, "away": 0.1}
    thinned = thin_timeline(points)
    assert len(thinned) < 25 and thinned[-1] is points[-1]
    assert points[49] in thinned and any(p is points[50] for p in thinned)


# --- 5. the post-match recap and the writer options --------------------------------------------------


def test_the_listener_writes_the_recap_at_the_final_whistle(demo):
    """Through the Service Bus loop and the threaded sink, the desk writes the recap once the final whistle is processed, from the approved cards only."""
    stream, _ = demo
    inner = MemorySink()
    sink = ThreadedSink(inner)
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True))
    clock, log = FakeClock(), {}
    consume(pipeline, fake_bus({"demo": bodies(stream)}, clock, log), idle_s=30, clock=clock)
    assert sink.close() == []
    recap = inner.recaps["demo-0007"]
    assert recap == pipeline.recaps["demo-0007"] and (recap["id"], recap["type"], recap["final"], recap["writer"]) == ("recap-demo-0007", "recap", True, "template")
    assert recap["verification"]["verified"] and recap["analysis"]["score"] == next(iter(pipeline.matches.values())).score()
    approved = {c["id"] for c in inner.cards.values() if c["status"] == "approved"}
    assert recap["cited_cards"] and set(recap["cited_cards"] + recap["analysis"]["cards_on_air"]) <= approved


def test_a_card_action_after_the_final_whistle_writes_the_recap_again(demo):
    """A goal the provider voids after full time: the card comes off and the recap is written again on the new score."""
    stream, truth = demo
    last = max((g for g in truth["goals"] if g["counts"]), key=lambda g: g["clock_s"])
    template = next(m for m in stream if m["type"] == "correction" and m["op"] == "void")
    seq, at = max(m["seq"] for m in stream), max(m["emitted_at_s"] for m in stream)
    void = {**template, "message_id": "extra-void", "seq": seq + 1, "emitted_at_s": at + 30, "target_event_id": last["event_id"], "cause": "provider_review", "related_message_id": None}
    sink = MemorySink()
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True))
    for m in stream:
        pipeline.handle(m)
    pipeline.flush()
    at_whistle = pipeline.recaps["demo-0007"]["analysis"]["score"]
    pipeline.handle(void)
    pipeline.flush()
    recap = sink.recaps["demo-0007"]
    side = last["team"]
    assert recap["analysis"]["score"][side] == at_whistle[side] - 1 and f"card-{last['event_id']}" in recap["analysis"]["cards_retracted"]
    assert recap["verification"]["verified"]


def test_the_recap_waits_for_the_whistle_itself(demo):
    """The last goal arrives late: the reorder buffer releases it with every message after it, the VAR check on it and the final whistle included, in one batch. The recap is written once, when the whistle itself is processed, and counts the goal."""

    class CountingDesk(Desk):
        written = 0

        def write_recap(self, cards, state, storyline=None):
            self.written += 1
            return super().write_recap(cards, state, storyline)

    stream, truth = demo
    feed = sorted({m["message_id"]: m for m in stream}.values(), key=lambda m: m["seq"])
    last = max((g for g in truth["goals"] if g["counts"]), key=lambda g: g["clock_s"])
    k = next(i for i, m in enumerate(feed) if m["type"] == "action" and m["event_id"] == last["event_id"])
    sink = MemorySink()
    desk = CountingDesk(sink, TemplateWriter(), auto_approve=True)
    pipeline = Pipeline(sink=sink, desk=desk)
    for m in feed[:k]:
        pipeline.handle(m)
    for m in feed[k + 1:]:
        assert pipeline.handle(m, now=feed[k]["emitted_at_s"]) == []  # held until the goal arrives
    released = pipeline.handle(feed[k])
    assert [r["action"] for r in released if "action" in r] and any(r.get("moment") == "goal" for r in released)
    assert desk.written == 1 and pipeline.recaps["demo-0007"]["final"]
    assert f"card-{last['event_id']}" in pipeline.recaps["demo-0007"]["analysis"]["cards_on_air"]
    pipeline.flush()
    assert desk.written == 1


def test_approvals_after_the_whistle_reach_the_recap_on_the_next_flush(demo):
    """Without auto-approve the cards wait for a producer, and the recap at the whistle cites none. The goals approved in the console after it are in the recap the next flush writes (the listener flushes when the session goes quiet); a flush with no new decision writes nothing."""

    class CountingDesk(Desk):
        written = 0

        def write_recap(self, cards, state, storyline=None):
            self.written += 1
            return super().write_recap(cards, state, storyline)

    stream, _ = demo
    sink = MemorySink()
    desk = CountingDesk(sink, TemplateWriter())
    pipeline = Pipeline(sink=sink, desk=desk)
    for m in stream:
        pipeline.handle(m)
    pipeline.flush()
    assert desk.written == 1 and pipeline.recaps["demo-0007"]["analysis"]["cards_on_air"] == []
    goals = [json.loads(json.dumps(c)) for c in sink.cards.values() if c["moment"] == "goal" and c["status"] == "pending"]
    console = Desk(sink)  # the console's desk, on the same store
    for card in goals:
        console.approve(card, "producer")
    pipeline.flush()
    recap = pipeline.recaps["demo-0007"]
    assert desk.written == 2 and sink.recaps["demo-0007"] == recap
    assert sorted(recap["analysis"]["cards_on_air"]) == sorted(c["id"] for c in goals) and recap["analysis"]["goals_consistent"]
    pipeline.flush()
    assert desk.written == 2


def test_a_flush_reads_back_only_the_matches_finished_within_the_window(demo):
    """A long-running worker flushes at every quiet session. It reads the cards of a finished match back for `recap_window_s` after the whistle, so an approval made then is in the recap; later, the match is left alone and a matchday of finished matches is not read again at each flush."""

    class CountingSink(MemorySink):
        reads = 0

        def load_card(self, match_id, card_id):
            self.reads += 1
            return super().load_card(match_id, card_id)

    stream, _ = demo
    clock, sink = FakeClock(), CountingSink()
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter()), clock=clock, recap_window_s=600)
    for m in stream:
        pipeline.handle(m)
    pipeline.flush()
    goals = [json.loads(json.dumps(c)) for c in sink.cards.values() if c["moment"] == "goal" and c["status"] == "pending"]
    console = Desk(sink)
    console.approve(goals[0], "producer")
    clock.now += 300
    reads = sink.reads
    pipeline.flush()
    assert sink.reads > reads and pipeline.recaps["demo-0007"]["analysis"]["cards_on_air"] == [goals[0]["id"]]
    console.approve(goals[1], "producer")
    clock.now += 600
    reads = sink.reads
    pipeline.flush()
    assert sink.reads == reads and pipeline.recaps["demo-0007"]["analysis"]["cards_on_air"] == [goals[0]["id"]]


def test_approvals_the_console_writes_survive_the_next_cards(demo, tmp_path):
    """Locally the console writes each approval into cards.json while the match goes on (here every card once, as soon as it is written): the worker's later writes keep them, and the recap counts them."""
    stream, _ = demo
    sink = JsonFileSink(tmp_path)
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter()))
    path = tmp_path / "demo-0007" / "cards.json"
    console = Desk(MemorySink())  # what the console's desk does to a card before writing it back
    approved = set()
    for m in stream:
        if not pipeline.handle(m):
            continue
        cards = json.loads(path.read_text(encoding="utf-8"))
        fresh = {c["id"] for c in cards if c["status"] == "pending" and (c["id"], c["version"]) not in approved}
        if fresh:
            approved |= {(c["id"], c["version"]) for c in cards if c["id"] in fresh}
            path.write_text(json.dumps([console.approve(c, "producer") if c["id"] in fresh else c for c in cards]), encoding="utf-8")
    pipeline.flush()
    stored = {c["id"]: c for c in json.loads(path.read_text(encoding="utf-8"))}
    kept = [stored[i]["status"] for i, version in approved if stored[i]["version"] == version]
    assert len(kept) >= 8 and set(kept) == {"approved", "retracted"}  # retracted: on air when VAR ruled the goal out
    assert sink.cards == stored
    on_air = {i for i, c in stored.items() if c["status"] == "approved"}
    assert set(pipeline.recaps["demo-0007"]["analysis"]["cards_on_air"]) == on_air


def test_the_final_whistle_is_processed_even_if_the_recap_fails(demo, caplog):
    class BrokenRecapDesk(Desk):
        def write_recap(self, cards, state, storyline=None):
            raise KeyError("draw_after_pct")

    stream, _ = demo
    sink = MemorySink()
    pipeline = Pipeline(sink=sink, desk=BrokenRecapDesk(sink, TemplateWriter(), auto_approve=True))
    with caplog.at_level(logging.WARNING, logger="worker"):
        for m in stream:
            pipeline.handle(m)
        pipeline.flush()
    assert sink.states["demo-0007"]["final"] and pipeline.recaps == {}
    assert any("recap of demo-0007 failed" in r.getMessage() for r in caplog.records)


def test_no_recap_when_it_is_turned_off_or_without_a_desk(demo):
    stream, _ = demo
    sink = MemorySink()
    off = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True), recap=False)
    for m in stream:
        off.handle(m)
    off.flush()
    bare, _ = offline(stream)
    assert off.recaps == sink.recaps == {} and bare.recaps == bare.sink.recaps == {}


class FakeContainer:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def upsert_item(self, item: dict) -> None:
        self.items.append(item)


def test_cosmos_sink_writes_the_recap_next_to_the_cards():
    sink = object.__new__(azure_io.CosmosSink)  # no client: the containers are all it uses
    sink.cards = FakeContainer()
    sink.recap({"id": "recap-m", "type": "recap", "match_id": "m", "texts": []})
    (item,) = sink.cards.items
    assert (item["id"], item["type"], item["match_id"]) == ("recap-m", "recap", "m")


class DocumentSink(MemorySink):
    """Keeps the events in a container, as Cosmos DB does, not in a list."""

    def __init__(self) -> None:
        super().__init__()
        self.events = FakeContainer()

    def event(self, m: dict) -> None:
        self.events.upsert_item(m)


MARGIN_AS_NUMBER = re.compile(r"(?i)\d\s?(?:cm|mm|m)\b|centim|millim|\bm[eè]tres?\b|\bmeters?\b|\bmetros?\b")  # a distance given as a number: never on air
CALL_WORDS = {"tight": re.compile(r"(?i)whisker|d'un rien|por muy poco|por un pelo"), "clear": re.compile(r"(?i)\bclear|hors-jeu net|\bnet\b|\bclaro\b")}  # how close, in words: may go on air


@pytest.mark.parametrize("clear", [False, True])
def test_the_offside_margin_stays_off_air_and_its_call_is_said_in_words(demo, tmp_path, clear):
    """The feed sends the margin of the offside VAR rules out: tight in the demo, clear when the same correction sends 60 cm. The margin stays in the card's var_decision, for the producer: the duo's lines on the ruling and the recap written at the whistle (recap.json) say offside and how close it was in words, from the call (by a whisker, or clear), never the margin as a number, whether the sink keeps the feed in a list or, like Cosmos DB, in a container."""
    stream, _ = demo
    void = next(m for m in stream if m["type"] == "correction" and m["op"] == "void" and m["cause"] == "var_offside")

    def gives_margin(m: dict) -> bool:
        return m["type"] == "correction" and m.get("related_message_id") == void["related_message_id"] and isinstance(m["changes"].get("offside"), dict)

    if clear:
        stream = [{**m, "changes": {**m["changes"], "offside": {**m["changes"]["offside"], "margin_m": 0.6}}} if gives_margin(m) else m for m in stream]
    margin = next(m["changes"]["offside"]["margin_m"] for m in stream if gives_margin(m))
    inner = DocumentSink()
    for sink in (JsonFileSink(tmp_path), ThreadedSink(inner)):
        pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True))
        for m in stream:
            pipeline.handle(m)
        pipeline.flush()
        if isinstance(sink, ThreadedSink):
            assert sink.close() == []
            cards, recap = inner.cards, inner.recaps["demo-0007"]
        else:
            cards = {c["id"]: c for c in json.loads((tmp_path / "demo-0007" / "cards.json").read_text(encoding="utf-8"))}
            recap = json.loads((tmp_path / "demo-0007" / "recap.json").read_text(encoding="utf-8"))
        decision = cards[f"card-{void['target_event_id']}"]["var_decision"]
        assert decision["margin_m"] == margin and decision["call"] == ("clear" if clear else "tight")  # for the producer
        ruled = [c for c in cards.values() if c.get("ruled_out")]
        assert len(ruled) == 2 and all(c["ruled_out"]["margin_cm"] is None and c["duo_ruling"] for c in ruled)
        lines = [l["line"] for c in ruled for l in c["duo_ruling"]]
        texts = [t[part] for t in recap["texts"] for part in ("recap", "fan", "analyst")]
        assert any("offside" in l.lower() for l in lines) and "offside" in next(t["recap"] for t in recap["texts"] if t["lang"] == "en")  # the reason is said
        call = "clear" if clear else "tight"
        assert any(CALL_WORDS[call].search(l) for l in lines) and CALL_WORDS[call].search(next(t["recap"] for t in recap["texts"] if t["lang"] == "en"))  # the call, in words
        figures = {f"{margin:g}", f"{margin:g}".replace(".", ","), f"{round(100 * margin)} cm", f"{round(100 * margin)}cm"}
        assert [x for x in lines + texts if MARGIN_AS_NUMBER.search(x) or any(f in x for f in figures)] == []  # never the margin as a number
        assert all(g["margin_cm"] is None for g in recap["analysis"]["disallowed_goals"]) and not [k for k in recap["facts"] if "margin" in k]
        assert [v for k, v in recap["facts"].items() if k.endswith("_offside_call")] == [call]


def test_loading_the_worker_loads_no_model_client():
    """Agent Framework and the Azure SDKs are imported only when a Foundry writer or an Azure sink is built."""
    code = ("import sys, worker.__main__, worker.pipeline, worker.desk, worker.recap, worker.azure_io, worker.insights; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in ('agent_framework', 'azure')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env={**os.environ, "PYTHONPATH": str(SRC)}).stdout
    assert out.strip() == "[]"


def test_cli_writes_the_recap_and_takes_the_writer_options(tmp_path, monkeypatch, capsys):
    from worker import __main__ as cli
    from worker import desk as desk_module

    made = []

    class SpyDesk(Desk):
        def __init__(self, *args, **kwargs):
            made.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(desk_module, "Desk", SpyDesk)
    replay = str(SRC.parent / "data" / "replay" / "demo-0007.jsonl.gz")
    monkeypatch.setattr(sys, "argv", ["worker", replay, "--writer", "template", "--auto-approve", "--desk-dir", str(tmp_path / "a")])
    cli.main()
    recap = json.loads((tmp_path / "a" / "demo-0007" / "recap.json").read_text(encoding="utf-8"))
    assert recap["id"] == "recap-demo-0007" and recap["verification"]["verified"]
    assert "recap    template, verified True" in capsys.readouterr().out
    assert isinstance(made[0]["insight_writer"], TemplateWriter) and made[0]["recap_writer"] is None  # by default the facts are written by template
    monkeypatch.setattr(sys, "argv", ["worker", replay, "--writer", "template", "--desk-dir", str(tmp_path / "b"), "--insights-writer", "writer", "--no-recap"])
    cli.main()
    assert made[1]["insight_writer"] is None and (tmp_path / "b" / "demo-0007" / "cards.json").exists() and not (tmp_path / "b" / "demo-0007" / "recap.json").exists()
