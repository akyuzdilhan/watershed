# Worker input and output: throughput, shutdown, robustness

Scope: `src/worker/match.py`, `pipeline.py`, `azure_io.py`, `__main__.py`, tests in `tests/test_worker_io.py`. Card behaviour is unchanged: the offline demo report (cards, actions, score against the truth) is byte for byte the same as on `main` and on `lot0`.

## Throughput

Observed before, from a laptop: about 4 minutes for the 1,761 messages of a match, that is about 135 ms per message, one blocking Cosmos DB upsert (event with a 22-player freeze frame) plus one blocking Service Bus completion, in sequence.

| Change | Where |
|---|---|
| `ThreadedSink`: events written by a pool (`--workers`, default 8); cards, card actions, audit entries and the match state go through one ordered lane, copied at hand-over (the desk keeps mutating the card dict). At most 128 writes in flight, beyond that the pipeline blocks. | `pipeline.py` |
| Messages received in batches of up to 100 (`receive_messages`, prefetch 100); a batch is processed, its writes drained, then its messages completed. A message is completed only once what it produced is stored. | `azure_io.consume` |
| Completion without waiting for the service acknowledgement (`fast_settle`, on by default). Since azure-servicebus 7.14 each `complete_message` waits for the outcome: one round trip per message, about 50 s per match from a laptop. A lost completion only causes a redelivery, which the Match drops as a duplicate. | `azure_io.listen` |
| Cosmos DB writes retried on 408, 429, 449, 5xx and connection errors, exponential backoff with jitter, honouring `x-ms-retry-after-ms`. | `azure_io.with_retry` |

Measured with fakes (no Azure): 400 messages through a sink taking 20 ms per write, 16 workers: about 0.6 s instead of 8 s sequential (test `test_events_are_written_concurrently`, asserts under a quarter of the sequential time).

Expected on Azure, not measured: events at 8 in parallel take 1,761 x 100 ms / 8, about 22 s, and completions become negligible. The real floor is the request units: the database shares 1,000 RU/s across its containers. At 10 to 20 RU per event upsert, a match costs 18,000 to 35,000 RU, so 18 to 35 s, with 429s absorbed by the SDK and `with_retry`. Target "well under a minute" is plausible but RU-bound; more workers will not help past that point.

Flag for the coordinator (contract owner): the `match_state` document carries the whole `wp_timeline` (one point per action, about 1,600 at full time, about 200 KB) and is upserted on every card, correction, period end and every 25 actions, about 100 times per match. That is roughly 20 MB of writes and possibly as many RU as all the events together. Writing it less often, or only the last points plus a final full write, would cut it; this needs a contract decision, so it is left as is.

## Pure pipeline

`Match.score()`, `reds()`, `added_time()` and the final-whistle check scanned every action or message on each call; `lot0` added a win-probability point per action, which calls `state()` on every action and made it quadratic.

The Match now keeps indexes updated in `_apply`: goals that count and red cards (event id to seq and team), added-time boards per period in the order applied, the lowest full-time seq, the highest seq and the last message with a clock. An index entry is recomputed from the action as it now stands whenever the action or a correction for it is applied, so late goals, voids that arrive before their goal, and outcome changes give the same answer as a full scan. `match_state()` uses the same indexes instead of sorting `by_seq`.

| Replay of the demo, pipeline only, best of 3 | Before | After |
|---|---|---|
| `main` (cards only) | 0.155 s | 0.025 s |
| `lot0` (with the per-action win-probability point) | 3.04 s | 0.70 s |

What is left (0.7 s) is the Skellam model in scipy (`winprob/skellam.py`, about 0.6 ms per prediction), outside this scope.

Equivalence is tested against the old scans on 20 unscripted seeds, shuffled (neighbour swaps, goals and corrections sent 80 to 300 messages late, 40 duplicates) with injected corrections the unscripted feed rarely sends (voided goals, a goal turned into a save, shots turned into goals, a voided red card), and on the demo. Removing the reindex on corrections makes 22 of these tests fail.

## Shutdown

Root cause: `listen` iterated `for msg in receiver`, which stopped after `idle_s` (30 s) without a message, then asked for the next session with `NEXT_AVAILABLE_SESSION` and `max_wait_time=30`. When no session is available, the service answers with a timeout that azure-servicebus maps to `OperationTimeoutError`, which is retryable: with the default `retry_total=3`, the open is tried 4 times, 30 s each, with backoffs of 1.6, 3.2 and 6.4 s in between (`_base_handler._do_retryable_operation`, `_open_with_retry` has no overall deadline). Total: 30 + 120 + 11 s, about 2 min 40 s, plus closing. The infra agent's diagnosis is the same. The AutoLockRenewer is not the cause: it checks its shutdown flag every 0.5 s and exits promptly.

Fix: the client is created with `retry_total=0` and `consume` handles retries itself. A session is released after `idle_s` without a message; the worker then looks once more for another session (2 s), and stops when no message has arrived for `idle_s`. Stop time after the last message: about idle_s + 2 s (test with a fake clock). Errors on `complete_message` or the session (lock lost, link detached) close the session and the loop takes the next one; unsettled messages come back and are dropped as duplicates. Errors opening a session are retried with backoff (0.5 s doubling up to 30 s).

`--forever` keeps waiting for new sessions (container mode); `--idle` sets the seconds. Ctrl+C: the messages already processed are completed once their writes are drained, the rest of the batch is abandoned, the receiver is closed and pending writes are drained.

## Robustness

- Unreadable messages (bad JSON, unknown schema, a body that is not an object, a missing field) are dead-lettered with reason `unreadable` and the error type, and processing goes on. An unreadable message no longer creates an empty Match entry.
- `ErrorLog` logs the first error of each kind and type, counts the others and prints a summary at the end: a Cosmos DB outage is two lines, not one per message.

## What is imperfect

- `fast_settle` sets a private attribute of the receiver (`_await_settlement_outcome`); a future SDK may rename it, in which case the worker silently falls back to awaited completions (slower, still correct). It also settles before the service confirms, as SDKs before 7.14 did.
- With `retry_total=0` the SDK no longer retries transient receive errors inside a session; the loop reopens the session instead, which costs a session accept (sub-second).
- A write that still fails after the retries is logged and counted, and the message is completed anyway: the event or card is lost in Cosmos DB until the next write of the same document. Cards are rewritten on each change, events are not.
- A KeyError or TypeError raised by a bug in the Spotter or the desk is treated as an unreadable message and dead-lettered, as before.
- Memory: a Match keeps every message with its freeze frame, about 10 MB per match; in `--forever` mode finished matches are never evicted. Fine for a matchday, a long-running container should be restarted between matchdays.
- Throughput on Azure is an estimate; it has not been measured.
