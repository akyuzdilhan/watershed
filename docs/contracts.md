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

In Cosmos DB the curve is thinned before each write (one point in ten, the last three, and both sides of every jump of 5 points or more), and the document is rewritten on every card or card action, at the end of each half, on a correction and every 25 actions. The local `state.json` keeps every point.

## Recap (`type: "recap"`, id `recap-<match_id>`)

Written after full time by `python -m worker.recap`: the post-match recap, fan and analyst texts in each language, the turning point and the facts they were checked against, built from approved cards only. Locally in `data/desk/<match_id>/recap.json`, in Azure in the `cards` container. Readers of `cards` must filter on `type`.

## Audit entry

Card actions from the Spotter: `{id, match_id, card_id, event_id, action (hold, release, retract, rewrite), cause, message_id, minute_label}`. Desk entries: `{id, match_id, card_id, action (written, approved, edited, rejected, retracted, rewrite_requested, hold, release), by, version, at}`; `by` is a model name, `template`, `feed`, `auto-approve` or the producer's identity.

## Producer decisions

`worker.desk.Desk` owns every status change: `approve(card, by, texts=None)` (with edited texts: an `edited` entry, then `approved`), `reject(card, by)`. A retracted card can never be approved. The console calls these methods; it never writes a status itself. The worker never overwrites a decision: before the feed changes a card (VAR check, correction) it reloads the stored card and keeps `status`, `texts`, `history` and `retracted_on_air` from it.

## Feed and CDF

The feed contract is in `src/generator/feed.py`; the at-rest export in `src/generator/cdf.py` (Common Data Format 0.3.2).

## Lot 2 additions

### History (`league.history`)

A fictional league history of 50 seasons, deterministic for a seed, stored at `data/history/<seed>/` (gitignored, regenerated on demand; a small sample may be committed for the demo). Players have a stable id `P000001` to `P100000`, a fictional name, a birth season, a position and a career (clubs by season). Matches carry the score, the scorers with minute, the assists, the substitutions with minute, the cards and the half-time score.

The worker and the insight engine only use this read API (a `History` object; tests may use a small fake with the same methods):

| Method | Returns |
|---|---|
| `seasons()` | list of season labels, oldest first (`"1976/77"` ... `"2025/26"`) |
| `club(name)` | `{name, founded_season, seasons_in_league, titles, best_finish}` |
| `head_to_head(home, away)` | list of past matches between the two clubs, newest first: `{season, matchday, home, away, home_goals, away_goals, half_time}` |
| `matches(where)` | every past match that satisfies `where(match) -> bool` (a full scan is fine: about 19,000 matches) |
| `player(player_id)` | `{id, name, position, born, clubs: [{season, club}], goals, appearances, sub_appearances}` |
| `count(event, where)` | how many past `event`s (`"match"`, `"goal"`, `"substitution"`, `"card"`) satisfy `where` |
| `last(event, where)` | the most recent such event, with its season and match, or None |

### Insight (`type: "insight"`, id `insight-<match_id>-<key>`)

A fact about the match in progress or just finished, compared with the history: `{id, type, match_id, key, kind (record, rarity, streak, milestone, storyline, tactical), minute_label, clock_s, event_id (or null), headline_facts: {field: value}, field_meanings: {field: meaning}, rarity: {p, surprisal_bits, basis}, value (0 to 100, the ranking key), status (proposed, pending, approved, rejected, superseded), texts (as cards), history}`. `rarity.basis` says what was counted ("4 goals by a substitute: 0 in 18,620 matches since 1976/77"). Every number shown comes from `headline_facts`. Insights go through the same Writer, Verifier and desk as cards.

### Storyline (`type: "storyline"`, id `storyline-<match_id>`)

Written before kick-off: the context of the match (table positions, stakes in % with explicit fields, form, head-to-head summary, players to watch, milestones within reach) as `facts` and `field_meanings`, plus the texts of a pre-match card.

### Commentary duo (`duo` field of cards and insights)

Two fictional commentators with complementary personalities, defined in `src/worker/duo.py` (no real person, no imitation of a real commentator). A card or insight may carry `duo: [{speaker, lang, line, claims}]`, two to four lines per language, every number claimed like `texts`. Personas: one carried by emotion and the story, one by the detail and the numbers.

Speakers are `wren` (Wren Calder, emotion and story) and `joss` (Joss Ferrier, detail and numbers); lines alternate. `duo_writer` says who wrote the lines (model name or `template`; a model's verified texts can come with template lines).

### Writer and desk fields (cards)

| Field | Type | Meaning |
|---|---|---|
| `on_air_priority` | `normal` or `low` | `low`: a goal that moves its team's chance of winning by less than 2 points; its texts give no probability (milestone or story line) |
| `scorer_match_goals` | int | goals cards: the scorer's goals in the match so far, this one included, goals ruled out excluded |
| `ruled_out` | `{reason, cause, minute_label, margin_cm}` | set by the desk when VAR voids a goal card: `reason` `offside`, `handball` or another cause, `minute_label` of the decision, `margin_cm` the offside margin in centimetres when the feed gives it (else null) |
| `duo_ruling` | array, as `duo` | what the duo says when VAR rules the goal out (templates only, verified) |
| `verification.repetition_warnings` | int | phrases, openings or tics already used in the match (warnings; an identical headline fails the draft) |

"Points" on screen means only a change in a team's chance of winning (`swing_pts`, `save_value_pts`, `turning_point_swing_pts`). `value_pts` ranks cards and is never written in a text.

Insights and the storyline are written by `Desk.write_insight(doc, teams)`: same Writer, Verifier, retry and template path, same statuses and audit entries as cards, stored through the sink's `insight` method when it has one, else `card`.

The recap (`type: "recap"`) adds `analysis.disallowed_goals`: `[{card_id, minute_label, team, player, reason, margin_cm}]`, read from the `ruled_out` records of retracted goal cards (or passed to `build_recap(..., disallowed=...)`); the texts name them only as ruled out, with the reason and the margin.
