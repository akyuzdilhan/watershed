"""Azure adapters: Cosmos DB as the sink, Service Bus as the source. Both authenticate with DefaultAzureCredential (az login locally, managed identity in Azure): no key, no connection string.

Cosmos DB containers `events`, `cards` and `audit` are partitioned by /match_id. Service Bus delivers each match on its own session (session id = match id), so one receiver gets a match in order; messages the worker cannot read go to the dead-letter queue instead of blocking the session.

The loop itself (`consume`) only sees a small receiver interface, so the tests drive it with fakes:
- messages arrive in batches; a batch is processed, its writes are drained, then its messages are completed, so a message is only completed once what it produced is stored;
- a session is released after `idle_s` seconds without a message; without `forever`, the worker stops when no session has had a message for `idle_s` seconds;
- Ctrl+C completes the messages already processed and stored, and abandons the others.
"""

import json
import logging
import os
import random
import time
from collections import Counter
from collections.abc import Callable

from worker.match import SchemaError
from worker.pipeline import Pipeline

log = logging.getLogger("worker")

TRANSIENT_STATUS = {408, 429, 449, 500, 502, 503, 504}


class ErrorLog:
    """Logs the first error of each kind, counts the others, and sums them up on demand: a Cosmos DB outage is one line, not one per message."""

    def __init__(self, logger: logging.Logger = log) -> None:
        self.logger = logger
        self.counts: Counter = Counter()

    def record(self, kind: str, error: BaseException) -> None:
        key = (kind, type(error).__name__)
        self.counts[key] += 1
        if self.counts[key] == 1:
            self.logger.warning("%s: %s: %s (further %s errors of this type are counted, not logged)", kind, type(error).__name__, str(error)[:300], kind)

    def summary(self) -> None:
        for (kind, name), n in sorted(self.counts.items()):
            self.logger.warning("%s: %d x %s", kind, n, name)


def is_transient(error: BaseException) -> bool:
    status = getattr(error, "status_code", None)
    if status is not None:
        return status in TRANSIENT_STATUS
    return type(error).__name__ in ("ServiceRequestError", "ServiceResponseError", "ConnectionError", "TimeoutError")


def with_retry(fn: Callable, attempts: int = 6, base_s: float = 0.5, max_s: float = 10.0, sleep: Callable[[float], None] = time.sleep):
    """Runs `fn`, retrying transient errors (throttling, unavailable, timeouts) with exponential backoff and jitter."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as error:
            if attempt == attempts - 1 or not is_transient(error):
                raise
            headers = getattr(error, "headers", None)
            hint = headers.get("x-ms-retry-after-ms") if hasattr(headers, "get") else None  # Cosmos DB says how long to wait on a 429
            delay = float(hint) / 1000 if hint else min(max_s, base_s * 2**attempt)
            sleep(delay * (1 + 0.2 * random.random()))


class CosmosSink:
    def __init__(self, endpoint: str, database: str) -> None:
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential

        db = CosmosClient(endpoint, credential=DefaultAzureCredential()).get_database_client(database)
        self.events, self.cards, self.audit = (db.get_container_client(name) for name in ("events", "cards", "audit"))

    @staticmethod
    def _upsert(container, item: dict) -> None:
        with_retry(lambda: container.upsert_item(item))

    def event(self, m: dict) -> None:
        self._upsert(self.events, {**m, "id": m["message_id"]})

    def card(self, card: dict) -> None:
        self._upsert(self.cards, card)

    def action(self, action: dict, card: dict) -> None:
        self._upsert(self.audit, action)
        self._upsert(self.cards, card)

    def audit_entry(self, entry: dict) -> None:
        self._upsert(self.audit, entry)

    def state(self, doc: dict) -> None:
        """The match state lives next to the cards, with type "match_state". Its curve is thinned (contract): every point would make a 200 KB document rewritten all match long."""
        self._upsert(self.cards, {**doc, "wp_timeline": thin_timeline(doc["wp_timeline"])})

    def insight(self, doc: dict) -> None:
        """Insights and the storyline live next to the cards, with type "insight" or "storyline" (contract)."""
        self._upsert(self.cards, {**doc, "type": "storyline" if doc.get("type") == "storyline" else "insight"})

    def load_insight(self, match_id: str, insight_id: str) -> dict | None:
        return self.load_card(match_id, insight_id)

    def load_card(self, match_id: str, card_id: str) -> dict | None:
        """The card as stored, with the producer's latest decision."""
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            return with_retry(lambda: self.cards.read_item(card_id, partition_key=match_id))
        except CosmosResourceNotFoundError:
            return None


def thin_timeline(points: list[dict], every: int = 10, keep: int = 3) -> list[dict]:
    """One point in `every`, the last `keep` points, and both sides of every jump of 5 points or more (a goal, a red card), so steps stay sharp."""
    picked = {i for i in range(0, len(points), every)} | set(range(max(len(points) - keep, 0), len(points)))
    for i in range(1, len(points)):
        if max(abs(points[i][k] - points[i - 1][k]) for k in ("home", "draw", "away")) >= 0.05:
            picked |= {i - 1, i}
    return [points[i] for i in sorted(picked)]


def cosmos_from_env() -> CosmosSink:
    return CosmosSink(os.environ["COSMOS_ENDPOINT"], os.environ.get("COSMOS_DATABASE", "watershed"))


MIN_WAIT_S = 2.0  # shortest wait for a session: the service needs a moment to hand one over


class NoSession(Exception):
    """No session had a message within the wait."""


UNREADABLE = (ValueError, KeyError, TypeError, AttributeError)  # bad JSON and SchemaError are ValueErrors


def _drain(sink, errors: ErrorLog) -> None:
    if hasattr(sink, "drain"):
        for error in sink.drain():
            errors.record("store", error)


def _process_batch(pipeline: Pipeline, receiver, batch: list, errors: ErrorLog, stats: Counter) -> None:
    done, settled = set(), set()
    try:
        for msg in batch:
            try:
                body = json.loads(str(msg))
                results = pipeline.handle(body)
            except UNREADABLE as error:
                receiver.dead_letter_message(msg, reason="unreadable", error_description=f"{type(error).__name__}: {error}"[:4000])
                settled.add(id(msg))
                errors.record("dead-lettered", error)
                stats["dead_lettered"] += 1
                continue
            for result in results:
                log.info("%s %s", result.get("action", "card"), result.get("card_id", result.get("id")))
            done.add(id(msg))
        _drain(pipeline.sink, errors)  # a message is completed once what it produced is stored
        for msg in batch:
            if id(msg) not in done:
                continue
            receiver.complete_message(msg)
            settled.add(id(msg))
            stats["completed"] += 1
    except KeyboardInterrupt:
        _drain(pipeline.sink, errors)
        for msg in batch:
            if id(msg) in settled:
                continue
            try:
                if id(msg) in done:
                    receiver.complete_message(msg)
                    stats["completed"] += 1
                else:
                    receiver.abandon_message(msg)
                    stats["abandoned"] += 1
            except Exception as error:  # the lock may already be gone: the message comes back, and is dropped as a duplicate
                errors.record("settle on interrupt", error)
        raise


def consume(pipeline: Pipeline, open_session: Callable[[float], object], idle_s: float = 30.0, forever: bool = False, batch_size: int = 100,
            errors: ErrorLog | None = None, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> Counter:
    """Receives sessions one after the other. `open_session(wait_s)` returns a receiver (a context manager) or raises NoSession."""
    errors = errors or ErrorLog()
    stats: Counter = Counter()
    last = clock()
    failures = 0
    probe = False  # after a session is released, look once for another before stopping
    try:
        while True:
            remaining = idle_s - (clock() - last)
            if not forever and remaining <= 0 and not probe:
                log.info("no message for %s s: stopping", idle_s)
                break
            probe = False
            try:
                receiver = open_session(idle_s if forever else max(remaining, MIN_WAIT_S))
            except NoSession:
                continue  # the loop decides whether to wait again or stop
            except KeyboardInterrupt:
                raise
            except Exception as error:
                errors.record("service bus", error)
                failures += 1
                sleep(min(30.0, 0.5 * 2**min(failures, 6)))
                continue
            failures = 0
            try:
                with receiver:
                    log.info("session %s", receiver.session.session_id)
                    while True:
                        batch = receiver.receive_messages(max_message_count=batch_size, max_wait_time=idle_s)
                        if not batch:
                            break  # the session is quiet: release it, another may have messages
                        last = clock()
                        stats["received"] += len(batch)
                        _process_batch(pipeline, receiver, batch, errors, stats)
                        if stats["received"] // 200 != (stats["received"] - len(batch)) // 200:
                            log.info("%d messages processed", stats["received"])
            except KeyboardInterrupt:
                raise
            except Exception as error:  # session lock lost, connection dropped: take a session again; unsettled messages come back
                errors.record("session", error)
            for result in pipeline.flush():
                log.info("%s %s", result.get("action", "card"), result.get("card_id", result.get("id")))
            _drain(pipeline.sink, errors)
            probe = True
    except KeyboardInterrupt:
        log.info("interrupted: in-flight messages completed or abandoned")
        stats["interrupted"] = 1
    _drain(pipeline.sink, errors)
    errors.summary()
    log.info("%s", dict(stats))
    return stats


def listen(pipeline: Pipeline, fqdn: str, topic: str, subscription: str = "worker", idle_s: float = 30.0, forever: bool = False,
           fast_settle: bool = True) -> Counter:
    """Receives sessions until none has a message for `idle_s` seconds (or forever)."""
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import NEXT_AVAILABLE_SESSION, AutoLockRenewer, ServiceBusClient
    from azure.servicebus.exceptions import OperationTimeoutError

    # retry_total=0: with NEXT_AVAILABLE_SESSION the SDK treats "no session within max_wait_time" as a retryable timeout and tries
    # three more times with backoff, which kept the worker alive for about 2.5 minutes after the last message. consume() retries itself.
    # The renewer keeps the session lock (one minute) alive while the Writer takes its time on a card.
    with ServiceBusClient(fqdn, credential=DefaultAzureCredential(), retry_total=0) as client, AutoLockRenewer(max_lock_renewal_duration=4 * 3600) as renewer:
        def open_session(wait_s: float):
            receiver = client.get_subscription_receiver(topic, subscription, session_id=NEXT_AVAILABLE_SESSION, max_wait_time=wait_s,
                                                        prefetch_count=100, auto_lock_renewer=renewer)
            if fast_settle and hasattr(receiver, "_await_settlement_outcome"):
                # Since 7.14 each complete waits for the service's acknowledgement: one round trip per message. A lost completion only
                # means a redelivery, which the Match drops as a duplicate, so the worker settles without waiting, as earlier versions did.
                receiver._await_settlement_outcome = False
            try:
                receiver.__enter__()
            except OperationTimeoutError as error:
                receiver.close()
                raise NoSession() from error
            except BaseException:
                receiver.close()
                raise
            return _Opened(receiver)

        return consume(pipeline, open_session, idle_s=idle_s, forever=forever)


class _Opened:
    """A receiver already opened by open_session: entering it again does nothing, leaving it closes it."""

    def __init__(self, receiver) -> None:
        self._receiver = receiver

    def __getattr__(self, name: str):
        if name == "_receiver":
            raise AttributeError(name)
        return getattr(self._receiver, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self._receiver.close()
