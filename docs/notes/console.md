# Producer console and public overlay

## What is built

| File | Role |
|---|---|
| `src/web/store.py` | Reads cards, match state and audit from JSON files (`DESK_DIR`) or Cosmos DB, cached 1 s per process. Decisions go through `worker.desk.Desk.approve/reject` with a sink writing back to the same store: `FileDeskSink` (replaces the card in cards.json atomically, appends to audit.jsonl) or the existing `worker.azure_io.CosmosSink`. The desk is imported only when a decision is made. |
| `src/web/console.py` | Page `/`: rundown sorted by `value_pts` with badges (pending, on air, rejected, retracted, retracted on air, VAR check), selected card (texts per language, Spanish labelled machine-translated, facts, verification, history from the audit), Approve / Edit / Reject, win-probability curve (three lines, card markers, retracted cards as grey crosses, half-time line, added time pushes the second half right), score and clock. Refresh every 1.5 s, nothing re-sent when the data did not change. |
| `src/web/overlay.py` | Page `/overlay?match=&lang=en|fr|es&clean=1`: lower-third with the latest approval still standing, a VAR tag while a check runs, the machine-translated tag, and "Goal ruled out: card withdrawn" (fr, es too) when a card that was on air is retracted. Imports `web.store` only: no worker, no Writer, no decision. |
| `src/web/security.py` | Per-IP sliding-window rate limit (default 120/min, `RATE_LIMIT_PER_MIN`) on `/`, `/overlay` and the Dash callback routes, 429 beyond; static assets not counted. IP = last `X-Forwarded-For` entry (the one the ingress appends). Sign-in gate: see below. |
| `src/web/app.py` | Router (`dcc.Location`), rate limit, callbacks. |
| `src/web/Dockerfile` | Build context = repository root; copies `src/` to `/app/src`, `PYTHONPATH=/app/src`, gunicorn `web.app:server` on port 80 (2 workers, gthread 4 threads). `Dockerfile.dockerignore` next to it trims the context. |
| `tests/test_web.py` | 18 tests: store, decisions and audit, edit refused by the Verifier, refresh and curve, overlay on air and retraction, overlay import isolation (subprocess), PUBLIC_MODE refusal through a real `_dash-update-component` POST, rate limiter. |

Edited texts are checked by the Verifier (`worker.writer.verify`) against the card's facts and its original claims before `Desk.approve(texts=...)`: a number that no fact backs is refused with the reason. No model call.

## Run locally

```
.venv\Scripts\python.exe -m worker data/replay/demo-0007.jsonl.gz --writer template --desk-dir data/desk
set DESK_DIR=data/desk
set PUBLIC_MODE=0
set PYTHONPATH=src
.venv\Scripts\python.exe -m web.app        # http://127.0.0.1:8050/ and /overlay (PORT to change)
```

The worker appends to `audit.jsonl`: delete `data/desk` before regenerating. Flask's `.env` loading is turned off in `python -m web.app`, otherwise the parent `.env` with `COSMOS_ENDPOINT` would switch the console to the cloud desk.

To see the overlay retraction: on the template desk the 63' card is retracted before anyone can approve it (the replay runs in one go), so nothing was on air. A desk that stops just after the retraction shows it: run the pipeline with `Desk(..., auto_approve=True)` and stop at the first `retract` (that is what `test_overlay_shows_the_retraction_of_a_card_that_was_on_air` does), or a live run.

## Environment for the infra agent

| Variable | Value |
|---|---|
| `DESK` | `cosmos` in Azure (Cosmos mode). Without it, `COSMOS_ENDPOINT` selects Cosmos only if `DESK_DIR` is not set. |
| `COSMOS_ENDPOINT`, `COSMOS_DATABASE` | endpoint; database `watershed` by default |
| `PUBLIC_MODE` | `1` (the image default). `0` only locally. |
| `EASY_AUTH_ENABLED` | `true` only when Easy Auth is configured on the container app. Otherwise nobody is a producer in public mode (the header could be forged). |
| `PRODUCERS` | optional comma-separated allow-list of user names (the `X-MS-CLIENT-PRINCIPAL-NAME` value) |
| `RATE_LIMIT_PER_MIN` | optional, default 120 |

Roles: the web app's managed identity needs **Cosmos DB Built-in Data Contributor** (SQL role `00000000-0000-0000-0000-000000000002`) on the account: it reads `cards` and `audit` and writes decisions. `DefaultAzureCredential` with a user-assigned identity needs `AZURE_CLIENT_ID`.

Easy Auth: Microsoft Entra provider, unauthenticated action **AllowAnonymous** (the app enforces sign-in on `/` and on the decision callbacks; `/overlay` must stay public). Login URL used by the console: `/.auth/login/aad?post_login_redirect_uri=/`.

Build: `azure.yaml` must build the web service from the repository root with `src/web/Dockerfile`. If ACR remote build ignores `Dockerfile.dockerignore`, a root `.dockerignore` excluding `.venv`, `.git`, `data/` (except what is needed), `.env*` keeps the context small and secrets out.

## What remains imperfect

- **Concurrent writers.** The worker keeps its own copy of each card. Locally, `JsonFileSink` rewrites the whole cards.json from memory on every card change, so a running worker overwrites the producer's decisions. In Cosmos, the worker's next upsert of a card (hold, release, retract) writes back its stale status, and `Desk.on_action` decides `was_on_air` from the worker's copy, which never sees a producer approval: a card approved in the console and then retracted is recorded as not on air, and the overlay will not show the retraction. Fix on the worker side: re-read the card from the store (or use the etag) before `on_action`, and patch only the fields it owns. This is the main gap for a live demo with a producer.
- Rate limit and cache are per process: with 2 gunicorn workers the effective limit is about 2 x 120/min per IP, and many viewers behind one NAT share a quota. The console uses about 40 to 80 requests a minute.
- Docker image not built here (Docker Desktop was not running); gunicorn does not run on Windows, so the WSGI entry `web.app:server` is only checked by import in the tests.
- The win-probability curve plots the timeline as recorded: a goal later ruled out still shows its spike, then the drop.
- The decision message stays under the buttons when another card is selected.
- The overlay picks the latest approval by audit order; with Cosmos the order uses `at` (desk entries) or `_ts`.
- Pitch views, match graphics, insights and storyline are in the console since lot 2: see `docs/notes/graphics.md`.
