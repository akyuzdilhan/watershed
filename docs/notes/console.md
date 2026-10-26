# Producer console and public overlay

## What is built

| File | Role |
|---|---|
| `src/web/store.py` | Reads cards, match state and audit from JSON files (`DESK_DIR`) or Cosmos DB, cached 1 s per process. Decisions go through `worker.desk.Desk.approve/reject` with a sink writing back to the same store: `FileDeskSink` (replaces the card in cards.json atomically, appends to audit.jsonl) or the existing `worker.azure_io.CosmosSink`. The desk is imported only when a decision is made. |
| `src/web/console.py` | Page `/`: rundown in match order with the cards waiting for a decision grouped on top (sorted here, `rundown_order`, whatever order the store returns), badges (pending, on air, rejected, retracted, retracted on air, VAR check), selected card with its heading, then Approve / Edit / Reject and the language switch (English by default, French, Spanish), then the texts of that language only; win-probability curve (three lines, card markers, retracted cards as grey crosses, half-time line, added time pushes the second half right), score and clock. The card detail: what the feed changed ("Scorer corrected: A → B", the VAR decision from `var_decision` or the desk's `ruled_out`, and on a card retracted on air what the lower-third said), on a card ruled out what the duo can say about the ruling (`duo_ruling`, no thumbs), then for the language picked the headline and why, the claims (value, the field in words with its key on hover, what the field means, from `worker.writer.card_facts`) of the text and of the suggested lines, and the Verifier's checks on the text as it stands (`verify`, `verify_duo`: numbers claimed, claims matching the facts, units, style, the shape of the suggested lines, warnings); then the suggested lines, the verdict when written with the repetition warnings and, when the card carries it, the writing trace (`verification.attempts_log`: writer, verified, rejected, finish reason, tokens in / out / reasoning, latency; for a producer only, folded under "Producer details", the error of each attempt, `fallback_reason` and `writer_config`), the history (actions and causes in words), the producer feedback. Refresh every 1.5 s, nothing re-sent when the data did not change; the detail is re-sent only when its card, its history or the feedback changed. |
| `src/web/overlay.py` | Page `/overlay?match=&lang=en|fr|es&clean=1`: lower-third with the latest approval still standing, a VAR tag while a check runs, "Scorer corrected" (fr "Buteur corrigé", es "Gol reasignado") on a card credited to another player, "auto-approved" (fr "validation automatique", es "aprobación automática") when the last approval of what is on air came from the desk's auto-approval, "AI-written, not reviewed" (fr "rédigé par IA, non relu", es "escrito por IA, sin revisar") on Spanish, which the model and the templates write directly with no native review, "Goal ruled out (VAR, offside)" (handball, foul; fr "But refusé (VAR, hors-jeu)", es "Gol anulado (VAR, fuera de juego)") when a card that was on air is retracted, never with the margin. At the final whistle the score bug reads "FT" and an element of the final whistle shows "FT 5-2" instead of "90+11'". A card holds the screen for 25 s of match clock, a retraction 20 s (below). Imports `web.store` only: no worker, no Writer, no decision, never the VAR margin. |
| `src/web/security.py` | Per-IP sliding-window rate limit (default 600/min, `RATE_LIMIT_PER_MIN`) on `/`, `/overlay` and the Dash callback routes, 429 beyond; static assets not counted. IP = last `X-Forwarded-For` entry (the one the ingress appends). Who may decide: see below. How the producer is named on screen (`display_name`): see "Producer details". |
| `src/web/labels.py` | Readable labels shared by the desk and the panels: `fact_label`, `field_label`, `action_label`, `readable`. |
| `src/web/app.py` | Router (`dcc.Location`), rate limit, callbacks. `/` is the console for everyone: read only without a producer. |
| `src/web/Dockerfile` | Build context = repository root; copies `src/` to `/app/src`, `PYTHONPATH=/app/src`, gunicorn `web.app:server` on port 80 (2 workers, gthread 4 threads). `Dockerfile.dockerignore` next to it trims the context. |
| `tests/test_web.py` | 55 tests: store, decisions and audit, edit refused by the Verifier, refresh and curve, the read-only console and the refusals through real `_dash-update-component` POSTs, claims and checks in the detail, scorer correction, VAR decision and margin, note drafts across ticks, overlay on air, hold and retraction (with the insight engine in auto-approve), import isolation (subprocess), rate limiter; the overlay tags (auto-approved, AI-written), FT at the final whistle, the VAR reason of a retraction and its exit after 20 s; the buttons above the texts, the language switch through the Dash endpoint for a visitor, the rundown order and the selection without a waiting card, readable labels, the ruling lines, the writing trace; the producer named on screen and audited by identity, the producer details folded and closed. |

Edited texts are checked by the Verifier (`worker.writer.verify`) against the card's facts and its original claims before `Desk.approve(texts=...)`: a number that no fact backs is refused with the reason. No model call.

## Read-only console (public mode)

With `PUBLIC_MODE=1` (the image default) and nobody signed in, `/` serves the whole console read only: the desk (rundown, card detail, curve) and the view switch to the match graphics and the insights and storyline, which `web.panels` draws (see the last section). The header says "Read-only view", with a "Producer sign-in" link to `/.auth/login/aad?post_login_redirect_uri=/` only when `EASY_AUTH_ENABLED` is true. The visitor gets no Approve, Edit or Reject (the buttons stay in the page, hidden and disabled, because the refresh callback sets them), no thumbs, reasons or notes, no producer identity in the history ("a producer"), no producer feedback statistics, no offside margin, no writer settings, attempt errors or `fallback_reason`. The refusal is on the server: every decision, edit and feedback callback asks `security.producer()` again and writes nothing without a producer (a decision or a feedback answers "Sign in to ...", the edit panel stays closed); the stylesheet also hides any `.actions`, `.fb` and `.fb-reasons` under `.read-only`, whatever a panel draws. With `PUBLIC_MODE=0`, locally, the local producer has every control.

The VAR decision of a card ruled out (`var_decision`: reason, `call` tight or clear, `margin_m`) reads "VAR decision: Offside, tight call"; the margin is added for a producer (signed in, or local) only, folded (below), and the overlay never shows it. The visitor never gets `fallback_reason` either: it can hold the error message of a service.

## Producer details

A screen capture of the producer's view (the demo video) shows neither an address nor a margin:

- The producer's identity (the Easy Auth user name) never shows. The console says `security.display_name()`, "Producer" or the value of `PRODUCER_DISPLAY_NAME`, in "Signed in as", in the decision and feedback messages ("On air, approved by Producer.") and in the history; the audit and the feedback documents keep the identity. The read-only view still says "a producer".
- What stays with the producer is folded under "Producer details" (`panels.producer_details`, an `html.Details` closed by default): in the card heading the offside margin ("Offside margin <metres> m: never on air or to visitors"), under the verification the error of each model attempt (out of the attempts table), `fallback_reason` and `writer_config` (deployment, effort, max_tokens, the 12 characters of `prompt_sha`); in the match graphics tab, under the note, the margin of the run picked (`panels.run_margin_details`); in the insights tab the producer-only facts (`PRODUCER_ONLY_FACTS`, the `margin_m` fact among them) and the rarity basis.
- Nothing that cannot be folded carries the margin, for the producer either: the run labels of the graphics picker, the insight titles and the run figures of both tabs (`show_margin=False`: no bracket, no metres in the annotation, the title or the meta). A visitor gets none of it, not even the summary line. A detail re-sent by the refresh (its card, history or feedback changed) closes again.

## Lower-third timing

When the desk auto-approves with the insight engine on, a goal card and its facts (brace, comeback) are approved in the same feed message, and the latest approval used to win: the lower-third showed the fact instead of the goal. Now a card holds the screen for `CARD_HOLD_S` (25 s) of match clock after it goes up, and while VAR checks it; a fact approved meanwhile shows on a detail line under the card ("The detail") and takes the screen once the card's time is up. The retraction of a card that was on air holds the screen the same way for `RETRACTION_HOLD_S` (20 s, a VAR check does not extend it) and then leaves it: the lower-third goes empty unless something was approved since. The match clock is the desk state's `clock_s`; when the card went up is its moment, moved later by the wall seconds between its writing and a producer's approval, or by the minute of a feed action on it since (a rewrite, a VAR check); a later period ends the hold. On the demo with auto-approve: 57' goal, detail "Varleigh draw level from 0-2 down", the fact alone at the next state write; 63' goal with its facts, held through the VAR check, then the retraction; 66' and 83' the same.

## Desk ergonomics

The detail is drawn in two outputs: `c-head` (heading, badges, corrections) above the bar with Approve, Edit, Reject and the language switch (`c-lang`), and `c-detail` (texts, ruling lines, suggested lines, verification, history) under it, so the decision is one click away from the heading and never below a screen of claims. The switch stays visible in the read-only view; the buttons do not. Both outputs are re-sent together, under the detail signature, which now includes the language. Without a selection the top card of the rundown is picked when it waits for a decision (the earliest waiting card), else the most recent card of the match (`default_pick`), not the first one.

Labels (`web.labels`, imported by the console and the panels, so `panels` never imports `console`): `fact_label` turns an insight key or kind into words ("ht-lead-lost-away-2" reads "Half-time lead lost", "streak" "Streak"), `field_label` a fact field ("varleigh_wp_before_pct" reads "Varleigh win probability before (%)"), `action_label` an audit action ("rewrite_requested" reads "Rewrite requested"). `console.fact_label` and `console.field_label` are the same functions. The insights tab shows the type of each fact in its list and its detail, the fields of the facts table in words with the key on hover, and labels Spanish "AI-written, not reviewed" like the desk and the overlay (`overlay.AI_WRITTEN_LABEL`).

## Note drafts

A note typed after a thumbs down used to vanish at the next refresh of the card. The refresh now keeps two signatures (`c-sig` holds `{all, detail}`): the rundown, curve and score follow every change, the detail is re-sent only when the selected card, its audit entries, the match's feedback or the selection changed. A tick that only moves the clock or another card leaves the field, and what is typed in it, as it is. The insights tab draws its own detail (`panels`), with its own refresh.

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

## Environment for the deployment

| Variable | Value |
|---|---|
| `DESK` | `cosmos` in Azure (Cosmos mode). Without it, `COSMOS_ENDPOINT` selects Cosmos only if `DESK_DIR` is not set. |
| `COSMOS_ENDPOINT`, `COSMOS_DATABASE` | endpoint; database `watershed` by default |
| `PUBLIC_MODE` | `1` (the image default): visitors get the console read only. `0` only locally. |
| `EASY_AUTH_ENABLED` | `true` only when Easy Auth is configured on the container app. Otherwise nobody is a producer in public mode (the header could be forged) and the console shows no sign-in link. |
| `PRODUCERS` | optional comma-separated allow-list of user names (the `X-MS-CLIENT-PRINCIPAL-NAME` value) |
| `PRODUCER_DISPLAY_NAME` | optional: how the console names the signed-in producer on screen, "Producer" by default. The audit keeps the user name. |
| `RATE_LIMIT_PER_MIN` | optional, default 600 per IP and per process. The desk (about 80 requests a minute), a graphics or insights panel (12) and the overlay (60) open together from one address send about 150: the former default of 120 answered 429 and left panels on "Loading." |

Roles: the web app's managed identity needs **Cosmos DB Built-in Data Contributor** (SQL role `00000000-0000-0000-0000-000000000002`) on the account: it reads `cards` and `audit` and writes decisions. `DefaultAzureCredential` with a user-assigned identity needs `AZURE_CLIENT_ID`.

Easy Auth: Microsoft Entra provider, unauthenticated action **AllowAnonymous** (`/` is read only without a producer and the app enforces sign-in on the decision and feedback callbacks; `/overlay` must stay public). Login URL used by the console: `/.auth/login/aad?post_login_redirect_uri=/`.

Build: `azure.yaml` must build the web service from the repository root with `src/web/Dockerfile`. If ACR remote build ignores `Dockerfile.dockerignore`, a root `.dockerignore` excluding `.venv`, `.git`, `data/` (except what is needed), `.env*` keeps the context small and secrets out.

## What remains imperfect

- **Concurrent writers.** The worker keeps its own copy of each card, and the console writes the producer's decisions into the store. Before the feed changes a card (a VAR check or a correction), the pipeline reads the stored card back and takes the fields the console may change (`Pipeline._refresh`, `PRODUCER_FIELDS`: `status`, `texts`, `history`, `retracted_on_air`), so a card approved in the console and then retracted is recorded as on air; locally `JsonFileSink` also keeps the producer's fields of every other document when it rewrites `cards.json` (`docs/notes/worker-io.md`, "Robustness"). A decision made between that read and the worker's write can still be overwritten; the window is the processing of one message (inference, not measured).
- Rate limit and cache are per process: with 2 gunicorn workers the effective limit is up to 2 x 600/min per IP, and many viewers behind one NAT share a quota (about four consoles with the overlay per address and process). Every visitor of the read-only console runs the desk refresh: the figure and the detail are built per viewer when the data changes.
- The lower-third hold reads the match clock from the desk state, which the worker writes on every card, correction and period end and every 25 actions: a card can hold one or two minutes rather than 25 s before its fact takes over. When a producer approves later, the moment of the approval comes from wall time, exact in the real-time windows of the replay, short outside them.
- The Verifier's repetition warnings are stored as a count per card (`verification.repetition_warnings`): the detail shows the count once, not per language. The checks per language are run again on the texts as they stand, so an edited text shows its own result.
- The image is built remotely in ACR by `azd deploy` and runs in Azure; gunicorn does not run on Windows, so the tests only check the WSGI entry `web.app:server` by import.
- The win-probability curve plots the timeline as recorded: a goal later ruled out still shows its spike, then the drop.
- The decision message stays under the buttons when another card is selected.
- The edit panel lists every language, whatever the language switch shows.
- The overlay reads the approvals in audit order (then applies the hold above); with Cosmos the order uses `at` (desk entries) or `_ts`.
- Pitch views, match graphics, insights and storyline are in the console: see `docs/notes/graphics.md`.
