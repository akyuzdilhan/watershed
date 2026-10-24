# Data contracts

What the worker writes and what the console, the overlay and the post-match analysis read. Change this file only together with every reader.

## Where the desk lives

| Mode | Cards and match state | Audit trail | How to select |
|---|---|---|---|
| Local | `data/desk/<match_id>/cards.json` (array), `state.json` | `data/desk/<match_id>/audit.jsonl` | `python -m worker <replay> --writer template --desk-dir data/desk`; readers use the `DESK_DIR` environment variable |
| Azure | Cosmos DB, database `watershed`, container `cards` (documents with `type` `card` or `match_state`) | container `audit` | `COSMOS_ENDPOINT` and `COSMOS_DATABASE` set; Entra ID only |

Every document carries `match_id`, the partition key in Cosmos DB.

## Card (`type: "card"`)

| Field | Type | Meaning |
|---|---|---|
| `id` | string | `card-<event_id>` |
| `match_id`, `event_id` | string | the action the card is about (for a save, the keeper's action) |
| `shot_event_id` | string or null | the shot behind a goal, a save or a chance |
| `moment` | `goal`, `save`, `red_card`, `chance` | |
| `status` | `proposed`, `pending`, `approved`, `rejected`, `retracted` | `pending`: written and checked, waiting for a producer; `approved`: on air |
| `retracted_on_air` | bool | set when a retracted card had been on air |
| `under_review` | bool | a VAR check is running on the moment |
| `version` | int | goes up when the card is rewritten (goal credited to another player) |
| `minute_label`, `clock_s` | string, number | match clock of the moment |
| `team`, `team_name`, `player` | `home` or `away`, string, `{id, shirt, name}` | the team and player the card credits |
| `score_before` | `{home, away}` | |
| `value_pts` | int | how far the moment moved the result probabilities, 0 to 100: the ranking key |
| `facts` | object | `xg_pct`, `team_wp_before_pct`, then `team_wp_after_pct` (goal, red card) or `team_wp_if_goal_pct` (save, chance), and the draw equivalents |
| `texts` | array of `{headline, why, lang, claims}` | one per language (`en`, `fr`, `es`); absent before the Writer |
| `machine_translated` | array of language codes | to label on air (`es`) |
| `writer` | string | model name or `template` |
| `verification` | `{verified, attempts}` | |
| `history` | array of `{action, by, version}` | short form of the audit trail |

## Match state (`type: "match_state"`, id `state-<match_id>`)

`teams` (`{home, away}` names), `score`, `reds`, `final`, `period`, `clock_s`, `minute_label`, `last_seq`, and `wp_timeline`: one point per action, `{seq, event_id, clock_s, minute_label, home, draw, away}` (probabilities after the action, between 0 and 1).

## Audit entry

Card actions from the Spotter: `{id, match_id, card_id, event_id, action (hold, release, retract, rewrite), cause, message_id, minute_label}`. Desk entries: `{id, match_id, card_id, action (written, approved, edited, rejected, retracted, rewrite_requested, hold, release), by, version, at}`; `by` is a model name, `template`, `feed`, `auto-approve` or the producer's identity.

## Producer decisions

`worker.desk.Desk` owns every status change: `approve(card, by, texts=None)` (with edited texts: an `edited` entry, then `approved`), `reject(card, by)`. A retracted card can never be approved. The console calls these methods; it never writes a status itself.

## Feed and CDF

The feed contract is in `src/generator/feed.py`; the at-rest export in `src/generator/cdf.py` (Common Data Format 0.3.2).
