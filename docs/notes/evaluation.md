# Evaluation harness: definitions and findings

`python -m worker.evaluate --seeds 100 --timing --out docs/evaluation.md [--json evaluation.json] [--bench data/bench]` runs the generator on unscripted seeds 1 to N plus the committed demo replay, the pipeline with the desk (template writer, auto-approve, in-memory sink), the Verifier, and replays every stream under extra perturbations. No Azure, no model call. The report is deterministic for a given seed range; only section 6 (processing time, `--timing`) changes from one run to the next. `tests/test_evaluate.py` checks it on seeds 1 to 3 and the demo.

## Feed time

Every message is handed to `Pipeline.handle(m, now=t)` with `t` its arrival time on the broadcast clock. Without perturbation `t = emitted_at_s`, which is what `Pipeline.handle(m)` uses by default. A result (card or card action) is stamped with the `t` of the message whose arrival released it from the reorder buffer; results released by the final `flush()` get the last arrival plus `hold_s`.

## Metrics

| Metric | Definition |
|---|---|
| Goal recall | Truth goals (`truth.goals`, disallowed ones included: their card must exist before it is retracted) with a card of kind `goal` on the same event id, over all truth goals. |
| Red card recall | Truth cards `red` or `second_yellow` with a `red_card` card on the same event id. |
| Planted save recall | Demo only: `save` moments of the truth (event id = the shot) with a `save` card whose `shot_event_id` is that shot. |
| Card precision against truth moments | Cards matching a truth goal, red card or planted save, over all cards. The unscripted truth plants no moment, so chance and save cards cannot match anything and pull this number down by design. |
| Card classes | `truth_goal`, `truth_red`, `truth_save` (above); `extra_chance` and `extra_save`: the card's shot is in `truth.shots` with the outcome the card claims (chance: not goal and not saved; save: saved) and, for a save, the keeper's action is the message right after the shot (`seq + 1`); `mispaired_save`: a save card whose keeper action is not that one; `ungrounded_*`: no matching truth shot. |
| Cards grounded | `truth_*` plus `extra_*`, over all cards. |
| Card actions | For each truth goal, expected `expected_card_action` (none, retract, rewrite) against what the Spotter did on its card: `retract`, `rewrite`, both in order, `none`, or `no card`. Correct when they are equal. |
| False retraction / rewrite | A retract (rewrite) on any card whose event is not a truth goal expecting a retract (rewrite). |
| VAR checks handled | Truth VAR checks on an event that has a card: a `hold`, then `release` if the decision is `stands`, `retract` if `overturned`. |
| Score | `Match.score()` at full time equals `truth.final_score`. |
| Value | `value_pts` of each card, by kind; bins 0-4, 5-9, 10-19, 20-29, 30-49, 50+. |
| Robustness | The stream is replayed with arrival times changed (messages untouched), sorted by arrival. Identical: same cards (kind, status, version, team, player, shot, value, facts, score before), same set of card actions (card, action, message id), same final score as the unperturbed run. Run without the desk. Perturbations: 0-2 s jitter on every message; 5 % duplicates resent 0-60 s later; 5 % delayed 0.5-2.5 s; a mix of the three (all within the 3 s buffer); beyond the buffer: 0-6 s jitter, 5 % delayed 4-10 s, 2 % delayed 30-120 s, and a targeted one where each goal that gets a void or reassign arrives 5 s after its correction (only matches with such a goal). Seeded with `random.Random(f"{name}-{seed}")`. |
| Action to card | Arrival time of the release minus the action's `wall_s` (for a save, the shot's `wall_s`). Split into provider latency (`emitted_at_s - wall_s` of the action message, first copy) and worker wait (release time minus `emitted_at_s`). |
| Card to retract / rewrite | Release time of the action minus release time of the card; also correction message `wall_s` to the action. |
| Demo window | For each demo moment expecting a retraction: card ready, card to hold (VAR check starts), card to retraction (VAR decision), and the check's length. This is the time a producer has to air the card before VAR takes it back. In the live replay `generator.publish.schedule` paces corrections (`min_correction_delay` 40 s), so the live window can differ from feed time. |
| Writer | Audit entries `written` with `verified` true, over all writes (first versions and rewrites); then the final texts of each card re-run through `verify()` with `card_facts()`; length limits; any `0%` or `100%` in a text. |
| Benchmark | `data/bench/*.jsonl` from `spikes/af_trial.py`, only read: per model and effort, runs, errors, JSON valid, cards verified (counted only when the run returned the right languages, as in `spikes/bench_report.py`), traps held, median seconds per card. `data/` is git-ignored, so the section only appears where the files exist; the committed report was generated with `--bench` pointing at a local `data/bench`. |

## Findings and their state

1. **A save could be credited to a later, unrelated save** (`worker/spotter.py`, `Spotter.saves`). A saved shot above the threshold used to wait for the next `keeper_save` of that team, with no check that it was the action right after the shot: when the shot reached the worker after its keeper action (one of the feed's own late messages), it was paired with a later save. Fixed: a save card pairs the shot only with the keeper action at `seq + 1`, whatever the arrival order; the report has no `mispaired_save` card. Test: `test_a_save_card_credits_the_keeper_action_right_after_its_shot`.
2. **A correction that arrived before its action was lost** (`worker/match.py`, `Match._apply`; `worker/spotter.py`, `process`). A `reassign`, `update` or `void` whose target had not been applied yet was dropped, so the card kept the first scorer or a voided goal stayed on air. Fixed: a correction that overtakes its action is applied when the action arrives. The targeted perturbation of the report (each corrected goal delivered 5 s after its correction) is identical on 18 of 18 matches and on the demo. Test: `test_a_goal_that_arrives_after_its_correction_ends_with_the_right_cards`.
3. **The buffer only releases on arrival** (`worker/match.py`, `release`). There is no timer: a gap is given up when a later message arrives at least `hold_s` after the oldest pending one was emitted, so a card can wait longer than `hold_s` (worker wait up to 7.3 s on 100 seeds, against a 3 s buffer). With few messages, for example during a stoppage, a held card waits for the next message; `Pipeline.flush()` is called when a Service Bus session goes quiet. Open.
4. **Processing time.** Predicting the win probability after every action for `wp_timeline` made the pipeline quadratic in the match length. The Match now keeps indexes of the goals, red cards, added time and final whistle (`docs/notes/worker-io.md`, "Pure pipeline"): the demo with the per-action point went from 3.04 s to 0.70 s, most of what is left being the Skellam model.
5. **Verifier name check** (`worker/writer.py`, `names_ok`). `verify` used to join headline and why, so the first word of `why` was read as a name mid-sentence. Fixed: headline and why are checked separately (`docs/notes/writer.md`); a name not in the facts now fails the draft.
6. **A message delayed far beyond the buffer can cost a card.** In the row "2 % of messages delayed by 30-120 s", seed 78 misses one card (`card-synthetic-0078-v2-a00023`). The cause has not been investigated; within the buffer, every perturbation leaves every match identical.

## Caveats

- The unscripted truth has no planted moments, so precision against truth moments mostly measures how many chance and save cards the 8-point threshold lets through; grounding is the better safety number for those cards.
- Seeds 1 to 100 contain no disallowed goal: the 29 VAR checks on carded moments all end with the goal standing. Retraction is therefore only exercised by the demo (2 cards); rewrites are exercised on 18 unscripted goals. More seeds, or a generator setting with more missed offsides, would be needed to measure retractions on unscripted play.
- In the demo truth, the `team` of a foul moment (`yellow-41`, `red-78`) is the fouled team, not the carded player's (`director.Moment.side`); the harness matches red cards on `truth.cards`, which carries the offender's team.
- Robustness runs use the pipeline without the desk; the desk only adds texts and statuses on top of the same Spotter results.
