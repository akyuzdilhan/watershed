# Producer feedback

The control room judges every sentence the desk writes, live, and the desk takes it into account for the next ones. A human decides, the system listens and keeps a trace.

## What is built

| File | Role |
|---|---|
| `src/web/panels.py`, `src/web/console.py` | Next to each headline, why and duo line, in every language shown (cards on the desk tab, insights and storyline on their tab): thumbs up and thumbs down. After a thumbs down, one-click reasons (wrong fact, not relevant, repetitive, wrong tone, too long, wrong moment, each a toggle) and a short note saved with Enter. Plain buttons: Tab and Enter or Space work. Under the card history, the approval rate of the match so far, by writer and language. |
| `src/worker/desk.py` | `Desk.feedback(doc, target, verdict, reasons, note, by, teams)`: stores a `feedback` document with the exact text judged, its writer, the document version and the facts it was written from, and an audit entry `action: "feedback"`. The document itself is not rewritten. |
| `src/web/store.py` | `Store.feedback` (through the desk), `Store.feedback_docs`. Locally `feedback.jsonl` per match; in Azure the `cards` container. A local console started from the environment also rebuilds `data/feedback/profile.json` after each feedback. |
| `src/worker/feedback.py` | Profile, prompt block, ranking weight, template demotion, measure, export, CLI. |
| `tests/test_feedback.py`, `tests/test_web.py` | Storage and audit, refusals, profile bounds, prompt, ranking with its floor, demoted variant, export shape, console controls with and without feedback, the public-mode refusal through a real callback POST. No model, no Azure. |

Feedback is a decision: in public mode an unauthenticated visitor gets "Sign in to give feedback", like approve. It never blocks approval: thumbs and approve are independent, and a feedback on the desk tab does not bump the console revision (that would close an edit in progress); the thumbs light up at the next 1.5 s tick, which already runs. The insights tab redraws at once. No new timer, no request per second.

## What the system does with it now

Everything below is in context: no weight of any model changes.

1. **Writer prompt.** `prompt_block` turns the profile into a bounded text (1,600 characters at most): up to 6 house rules (from the reasons and the notes, the most frequent then the most recent first) and up to 4 rejected and 4 approved sentences (the same moment or family and the requested languages first). The prompt says these are the control room's preferences and that the facts and the Writer rules still win. `Desk` passes it to `FoundryWriter.write_async(preferences=...)`; `writer_prompt` builds the message.
2. **Insight ranking.** Per family (and per card moment), the approval rate is smoothed towards a prior (beta prior of 4 votes at 75 %). Below 5 votes nothing changes. After that, the value of a new insight fact is multiplied by `rate / 0.75`, clamped between 0.6 (floor) and 1.1. A family the producers keep turning down drops in the ranking and can fall under `min_value`; a scorer or ten-men fact that reached `min_value` stays at `min_value` or above, so feedback never removes a goal or a red card. The weight shows on the insight as `feedback_weight`.
3. **Templates.** Each feedback stores the sentence's skeleton (fact names as `{}`, numbers as `#`). A template skeleton with at least 2 thumbs down and more downs than ups is demoted: the picker scores it after every other variant, before a variant already said in the match. Model sentences are never demoted this way (they go to the prompt examples).
4. **Measure.** The console shows the approval rate of the match so far, by writer and language; `python -m worker.feedback stats` prints it for a desk.

The worker uses a profile only when asked: `FEEDBACK_PROFILE` set (or `Desk(profile=...)`, `InsightEngine(profile=...)`). It then reads the file again when it changes (one `stat` per draft), so feedback given during a match reaches the next drafts without a restart, as long as the worker and the console share the folder. Locally:

```
set FEEDBACK_PROFILE=data/feedback/profile.json
.venv\Scripts\python.exe -m worker data/replay/demo-0007.jsonl.gz --writer template --desk-dir data/desk
.venv\Scripts\python.exe -m worker.feedback build --desk-dir data/desk     # rebuild by hand (the local console does it after each feedback)
.venv\Scripts\python.exe -m worker.feedback export --out data/feedback/dataset.jsonl
```

## What "learning" means here, and what it does not

It means the desk adapts its next outputs to the stored judgements: what it is told (prompt), what it ranks (insights), what it picks (templates). It does not mean the model learns: there is no fine-tuning, no gradient, no model trained on the producers' data during the hackathon. A model given examples in its prompt may follow them or not; the Verifier still checks every number. With few votes the effect is small by construction (prior, minimum votes, floor).

## Preparing a later fine-tuning or an evaluation set

`python -m worker.feedback export --out data/feedback/dataset.jsonl` writes:

- one `judgement` line per sentence judged (latest verdict): facts, field meanings, the sentence, part, language, verdict, reasons, note, writer, document and version. The producer's identity is left out.
- one `pair` line per approved and rejected sentence of the same document, part and language (another version, another writer): `chosen`, `rejected`, the shared facts and field meanings, and the reasons for the rejection.

A later step could use the pairs for preference tuning (DPO or a reward model: prompt = the facts and field meanings with the Writer instructions, chosen, rejected), and the judgements as an evaluation set: replay the facts through a new Writer and measure how often it would have been approved, with the Verifier as a hard gate. That needs far more votes than a demo produces, a split by match to avoid leakage, and a check that the preferences of one producer do not become the house style by accident.

## Limits

- Small numbers: a profile from a demo has tens of votes. The prior and the floor keep it from swinging the desk; they also mean the ranking hardly moves before 5 votes per family.
- Cards are ranked by the Spotter (`value_pts`, not owned here): card moment rates are measured and shown, not applied to the rundown.
- In Azure the feedback is in Cosmos, but the profile is a local file: `python -m worker.feedback build --cosmos` writes it from Cosmos; the worker container does not rebuild it on its own.
- A note being typed is lost if the desk redraws the card at that moment (a new card or a feed change); the edit panel does not have this problem because it is drawn apart.
- A rewritten sentence starts with fresh thumbs: a verdict is shown only on the exact text it judged.
- The skeleton matches template variants by text: two variants that differ only by a name or a number share it.
- The profile is opt-in (`FEEDBACK_PROFILE`): without it, the worker writes exactly as before, which keeps the other tests and the evaluation deterministic. Forgetting to set it for the demo worker means the feedback is stored and measured but not used.
