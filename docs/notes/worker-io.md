# Worker input and output: throughput, shutdown, robustness

Scope: `src/worker/match.py`, `pipeline.py`, `azure_io.py`, `__main__.py`, tests in `tests/test_worker_io.py`. The throughput, shutdown and robustness changes below left card behaviour unchanged: the offline demo report (cards, actions, score against the truth) stayed byte for byte the same.

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

Open point: the `match_state` document carries the whole `wp_timeline` (one point per action, about 1,600 at full time, about 200 KB) and is upserted on every card, correction, period end and every 25 actions, about 100 times per match. That is roughly 20 MB of writes and possibly as many RU as all the events together. Writing it less often, or only the last points plus a final full write, would cut it; this needs a contract decision, so it is left as is.

## Pure pipeline

`Match.score()`, `reds()`, `added_time()` and the final-whistle check scanned every action or message on each call; the win-probability point added per action calls `state()` on every action, which made it quadratic.

The Match now keeps indexes updated in `_apply`: goals that count and red cards (event id to seq and team), added-time boards per period in the order applied, the lowest full-time seq, the highest seq and the last message with a clock. An index entry is recomputed from the action as it now stands whenever the action or a correction for it is applied, so late goals, voids that arrive before their goal, and outcome changes give the same answer as a full scan. `match_state()` uses the same indexes instead of sorting `by_seq`.

| Replay of the demo, pipeline only, best of 3 | Before | After |
|---|---|---|
| Cards only | 0.155 s | 0.025 s |
| With the per-action win-probability point | 3.04 s | 0.70 s |

What is left (0.7 s) is the Skellam model in scipy (`winprob/skellam.py`, about 0.6 ms per prediction), outside this scope.

Equivalence is tested against the old scans on 20 unscripted seeds, shuffled (neighbour swaps, goals and corrections sent 80 to 300 messages late, 40 duplicates) with injected corrections the unscripted feed rarely sends (voided goals, a goal turned into a save, shots turned into goals, a voided red card), and on the demo. Removing the reindex on corrections makes 22 of these tests fail.

## Shutdown

Root cause: `listen` iterated `for msg in receiver`, which stopped after `idle_s` (30 s) without a message, then asked for the next session with `NEXT_AVAILABLE_SESSION` and `max_wait_time=30`. When no session is available, the service answers with a timeout that azure-servicebus maps to `OperationTimeoutError`, which is retryable: with the default `retry_total=3`, the open is tried 4 times, 30 s each, with backoffs of 1.6, 3.2 and 6.4 s in between (`_base_handler._do_retryable_operation`, `_open_with_retry` has no overall deadline). Total: 30 + 120 + 11 s, about 2 min 40 s, plus closing. The AutoLockRenewer is not the cause: it checks its shutdown flag every 0.5 s and exits promptly.

Fix: the client is created with `retry_total=0` and `consume` handles retries itself. A session is released after `idle_s` without a message; the worker then looks once more for another session (2 s), and stops when no message has arrived for `idle_s`. Stop time after the last message: about idle_s + 2 s (test with a fake clock). Errors on `complete_message` or the session (lock lost, link detached) close the session and the loop takes the next one; unsettled messages come back and are dropped as duplicates. Errors opening a session are retried with backoff (0.5 s doubling up to 30 s).

`--forever` keeps waiting for new sessions (container mode); `--idle` sets the seconds. Ctrl+C: the messages already processed are completed once their writes are drained, the rest of the batch is abandoned, the receiver is closed and pending writes are drained.

## Robustness

- Unreadable messages (bad JSON, unknown schema, a body that is not an object, a missing field) are dead-lettered with reason `unreadable` and the error type, and processing goes on. An unreadable message no longer creates an empty Match entry.
- `ErrorLog` logs the first error of each kind and type, counts the others and prints a summary at the end: a Cosmos DB outage is two lines, not one per message.

## Recap at the final whistle

With a desk, the pipeline asks the desk for the post-match recap once it has processed the `full_time` message, and again whenever a later message produces a card or a card action (a goal voided after the whistle). The cards are read back from the store first (`load_card`), so the recap sees the producer's approvals made in the console; only approved cards count (`worker.recap`). The desk writes it with its `recap_writer` (the recap agent on Foundry with `--writer foundry`, the template otherwise) and stores it through the sink:

| Sink | Where |
|---|---|
| `MemorySink` | `recaps[match_id]` |
| `JsonFileSink` | `<desk-dir>/<match_id>/recap.json`, the same bytes `python -m worker.recap <desk-dir>/<match_id>` writes (test `test_the_worker_writes_the_recap_the_cli_would_write`) |
| `CosmosSink` | container `cards`, `type: "recap"`, id `recap-<match_id>`, as `python -m worker.recap --cosmos` writes it |
| `ThreadedSink` | the ordered lane, after the cards |

`--no-recap` turns it off. A recap that fails is logged (`recap of <match> failed`) and the final whistle is processed anyway: losing it would not be worth dead-lettering the `full_time` message. Approvals made after the whistle are not in the recap until `python -m worker.recap` runs again.

## Model calls per match

One budget per match, `Desk.max_calls_per_match` (40), for every draft the desk sends to a model; a draft costs one call, two when the Verifier sends it back, and the templates write once the budget is spent.

| Document | Written by, by default | Calls |
|---|---|---|
| Card (and its duo) | `--writer` | 1 per card, 2 with the retry; a rewrite is a new draft |
| Recap | the recap agent with `--writer foundry`, else the template | 1, 2 with the retry, from what the cards left |
| Insight, storyline | the templates (`--insights-writer template`) | 0; with `--insights-writer writer`, as a card |
| Duo after a VAR ruling (`duo_ruling`) | the templates | 0 |

On the demo with `--writer foundry`: 12 card drafts (11 cards and one rewrite) and one recap, 13 calls when every draft passes the first time, 26 at most. With `--insights-writer writer` and the seed-9 history, the 16 insight drafts and the storyline come on top (29 drafts before the recap), and the budget can run out before the recap.

## Audit trail: one entry per step

Before, a card action from the feed was written twice when a desk was running: the Spotter's line (`hold`, `retract`...) through `sink.action`, then the desk's entry (`hold`, `retracted`...). With a desk, the pipeline now hands the action to the desk only; the desk's entry keeps the action's `event_id`, `message_id`, `minute_label` and `cause`, and `score_against_truth` reads either form. Without a desk the Spotter's line is still the record. The insight engine writes its own steps (`proposed`, `updated`, `revived`, `superseded` with `cause`, `superseded_by` and `was_on_air`) through the sink's `audit_entry`, so a fact taken back by VAR is in the audit trail; before, it was only in the insight's `history`. With a desk, the audit entries of a card or an insight are the steps of its `history`, in order (tests `test_each_card_action_is_audited_once`, `test_every_step_of_a_fact_is_audited_once`).

## What is imperfect

- `fast_settle` sets a private attribute of the receiver (`_await_settlement_outcome`); a future SDK may rename it, in which case the worker silently falls back to awaited completions (slower, still correct). It also settles before the service confirms, as SDKs before 7.14 did.
- With `retry_total=0` the SDK no longer retries transient receive errors inside a session; the loop reopens the session instead, which costs a session accept (sub-second).
- A write that still fails after the retries is logged and counted, and the message is completed anyway: the event or card is lost in Cosmos DB until the next write of the same document. Cards are rewritten on each change, events are not.
- A KeyError or TypeError raised by a bug in the Spotter or the desk is treated as an unreadable message and dead-lettered, as before.
- Memory: a Match keeps every message with its freeze frame, about 10 MB per match; in `--forever` mode finished matches are never evicted. Fine for a matchday, a long-running container should be restarted between matchdays.
- Throughput on Azure is an estimate; it has not been measured.
