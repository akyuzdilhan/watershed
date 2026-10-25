# Samples from a live run

Empty for now. This folder will hold documents copied as they are from the desk of a live run with the Foundry Writer and the production prompt (the pass measured in `docs/evaluation.md`, "Live run, production prompt"), so that a reader can see what the model actually wrote and what the Verifier did with it. Nothing here is written by hand or edited.

What will go here:

| File | Content |
|---|---|
| `cards.json` | A few cards written by the model and verified, with their `texts` (each with its `claims`), `duo`, `verification` (with `attempts_log`) and `writer_config` |
| `retried-card.json` | One card whose first reply the Verifier rejected and whose second reply it passed: its `attempts_log` keeps what the first attempt was rejected for (`rejected`), and its texts are those of the second |
| `measure.md` | The output of `python -m worker.measure` for the same run |
| `README.md` | This file, with the date, the match, the deployment, the effort and the `prompt_sha` of the run once it exists |

The players and teams are synthetic (seeded generator), so the samples contain no personal data. Copy the documents from `data/desk/<match_id>/` or from Cosmos DB without changing them; if a name is regenerated later, take new samples from a new run rather than editing these.
