# Post-match recap and audience previews (`worker.recap`)

## What

`src/worker/recap.py` turns the desk of a finished match (`cards.json` and `state.json` as written by `JsonFileSink`) into `recap.json`: a deterministic analysis, then three texts per language (`en`, `fr`, `es`, the last labelled machine-translated).

- **Input.** Only `approved` cards are read. Retracted, rejected and pending cards are never cited. The points of `wp_timeline` that belong to a retracted moment are dropped: a voided goal leaves a one-point spike on the live curve (63' and 69' in the demo).
- **Analysis** (`analyse`). Final and half-time score (the half-time score is given only when the approved goals add up to the final score), the turning point (the approved goal or red card that raised the eventual winner's chance of winning the most; in a draw, the one that moved either side's chance the most), the 3 biggest moments by `value_pts`, a per-team summary (goals, red cards, scorers with minutes, saves and cards on air), and the key points of the curve (kick-off, half-time, the winner's lowest point, the minute from which the winner stayed above 50% for good, final). Win probabilities at moments come from the card facts (as aired, clamped by `shown`); the other side's is 100 minus the card side's and the draw.
- **Facts** (`recap_facts`). A flat dict with explicit field names (`rovers_lowest_wp_pct`, `turning_point_rovers_wp_after_pct`, `disallowed_1_margin_cm`...; `value_pts` only orders the biggest moments and is no longer a fact) plus their meanings, like `card_facts`.
- **Texts.** `recap` (max 60 words, neutral), `fan` (max 50, emotional, from one side: the winner by default, `--fan-team` to change), `analyst` (max 60, numbers-led, following the winner, or home in a draw).
- **Writers.** `RecapWriter`: Agent Framework agent on Foundry, same set-up as `FoundryWriter`, with a `get_recap_facts(match_id)` function tool. `RecapTemplateWriter`: fixed sentences, each used only when every field it names is in the facts; trailing sentences are dropped if a text goes over its limit. Flow: model, one retry (with the list of failures as a note), then the template.
- **Verifier** (`verify_recap`). Runs `writer.verify` on each text (every number claimed, claims match their field, % from `_pct` fields, no decimal probability), then adds: length per section, `names_ok` must be empty (in `writer.verify` it is reported but not blocking), no player of the match whose name is not in the facts, no spelled-out numbers (two/deux/dos...), no minute of a retracted card.

On the demo, the turning point is Lightfoot's 66' goal (Rovers 28% to 79%, 51 points), the biggest moments are 66', 57' and 14', and the texts are verified in the three languages.

## How to run

```
python -m worker data/replay/demo-0007.jsonl.gz --writer template --auto-approve --desk-dir data/desk
python -m worker.recap data/desk/demo-0007                       # template writer, writes data/desk/demo-0007/recap.json and prints it
python -m worker.recap data/desk/demo-0007 --writer foundry      # needs FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL (.env); paid calls, not run so far
python -m worker.recap data/desk/demo-0007 --fan-team away --out recap-united.json
python -m worker.recap --cosmos demo-0007                        # reads cards and match state from Cosmos DB, upserts the recap next to them
```

The `.venv` is in the main checkout and the package is installed in editable mode from there: from a worktree, set `PYTHONPATH=src` so the worktree's code is the one imported. Tests: `pytest tests/test_recap.py` (template writer only; a fake model checks the retry then fallback path).

## What is imperfect

- **Cosmos mode not tested.** `recap_cosmos` writes a document with `type: "recap"` into the `cards` container. `docs/contracts.md` only lists `card` and `match_state` there: a reader that does not filter on `type` would choke. The contract needs a "Recap" section (I did not touch that file); the console must filter on `type`.
- **Foundry path never run.** The code mirrors `FoundryWriter`, but the prompt has not been tried on a model. The 60/50/60 word limits are tight for French with three texts in one call.
- **Turning point = goals and red cards only.** A save or a missed chance does not move the win probability; it can appear in the biggest moments (by `value_pts`, which is counterfactual) but never as the turning point.
- **Rounding.** The opponent's chance at a moment is 100 minus two rounded values: it can be 1 point off the curve.
- **Goals ruled out by VAR** are now told, only as ruled out, with reason and margin (`disallowed_*` facts, from the `ruled_out` record of the desk; see `docs/notes/duo.md`). A sentence naming a retracted minute must also say VAR or ruled out.
- **Name check is heuristic.** `names_ok` flags capitalised words that are not in the facts and not at the start of a sentence; a correct word that happens to be capitalised mid-sentence (Premier League, VAR) fails the draft and sends it to the template. Players are only known from the cards, so a player with no card at all cannot be detected by `unknown_players` (`names_ok` still catches him).
- **Spelled-out numbers** are checked from a small word list; "hat-trick", "a brace" or "ten men" (blocked) are crude edges.
- **Templates.** Simple and repetitive ("Rovers 5-2 United" right after "for Rovers"), no gender agreement in French (`Rovers est tombé`). The "(N pts)" of the analyst text is gone: points on screen only mean a change in a chance of winning. The fan text in a draw can read flat after a late equaliser.
- **Half-time** is detected as the clock going back by more than 120 s; a match stopped before the second half has no half-time point.
- **`writer.verify` reuse.** It hard-codes `headline`/`why` and the card lengths, so each recap text is passed as a `headline` and its `length_ok` is ignored. A cleaner split would be a `verify_text(body, claims, facts)` helper in `writer.py` that both use; not done since I don't own that file.
