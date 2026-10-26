# Data contracts

What the worker writes and what the console, the overlay and the post-match analysis read. Change this file only together with every reader.

## Where the desk lives

| Mode | Cards and match state | Audit trail | How to select |
|---|---|---|---|
| Local | `data/desk/<match_id>/cards.json` (array), `state.json`, `insights.json`, `storyline.json`, `recap.json` | `data/desk/<match_id>/audit.jsonl` | `python -m worker <replay> --writer template --desk-dir data/desk`; readers use the `DESK_DIR` environment variable |
| Azure | Cosmos DB, database `watershed`, container `cards` (documents with `type` `card`, `match_state`, `insight`, `storyline`, `recap` or `feedback`) | container `audit` | `COSMOS_ENDPOINT` and `COSMOS_DATABASE` set; Entra ID only |

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
| `facts` | object | `xg_pct`, `team_wp_before_pct`, then `team_wp_after_pct` (goal, red card) or `team_wp_if_goal_pct` (save, chance), and the draw equivalents; the win and draw percentages are between 1 and 99, as on air (a moment is always before the final whistle) |
| `card_kind` | `red` or `second_yellow` | red card cards only: the card the feed shows on the foul; absent on other cards |
| `previous_player` | string | a rewritten card (version 2 or more) whose player changed: the name credited by the previous version; absent when the rewrite keeps the player, and on a card never rewritten |
| `var_decision` | `{reason, call, margin_m}` | set by the worker when a correction voids the card (and kept on the card, so it reaches Cosmos DB): `reason` the cause of the correction (`var_offside`, `var_handball`, or another cause such as `provider_review`), `margin_m` the offside margin in metres the feed sends on the pass in a correction tied to the same VAR decision (else null), `call` `tight` or `clear` from that margin (`writer.offside_call`: tight up to 30 cm), else null. The margin and the call are for the producer only: never on air, never shown to anonymous visitors (in public mode the console's API and panels leave out `margin_m` and `call`; the worker writes them on the card for the signed-in producer). Neither the margin nor the call is said on air (no "by a whisker", "clear"): the desk does not copy them into `ruled_out`, which the duo and the recap read |
| `texts` | array of `{headline, why, lang, claims}` | one per language (`en`, `fr`, `es`); absent before the Writer |
| `machine_translated` | array of language codes | to label on air (`es`) |
| `writer` | string | model name or `template` |
| `verification` | `{verified, attempts}` | |
| `history` | array of `{action, by, version}` | short form of the audit trail |

## Match state (`type: "match_state"`, id `state-<match_id>`)

`teams` (`{home, away}` names), `score`, `reds`, `final`, `period`, `clock_s`, `minute_label`, `last_seq`, and `wp_timeline`: one point per action, `{seq, event_id, clock_s, minute_label, home, draw, away}` (probabilities after the action, between 0 and 1).

In Cosmos DB the curve is thinned before each write (one point in ten, the last three, and both sides of every jump of 5 points or more), and the document is rewritten on every card or card action, at the end of each half, on a correction and every 25 actions. The local `state.json` keeps every point.

## Recap (`type: "recap"`, id `recap-<match_id>`)

The post-match recap, fan and analyst texts in each language (`en`, `fr`, `es`), the turning point and the facts they were checked against, built from approved cards only (`worker.recap.build_recap`). The worker's desk writes it at the final whistle, from the cards with the producer's decisions read back from the store, and writes it again when the feed changes a card after the whistle or when the worker flushes (a Service Bus session going quiet, the end of a replay) within an hour of the whistle (`Pipeline.recap_window_s`) and finds a card whose status or version changed since (`python -m worker ... --no-recap` turns it off; there is no recap without a desk). `python -m worker.recap` writes the same document on demand, for instance after approvals made once the worker has stopped. Locally in `data/desk/<match_id>/recap.json` (the format of `python -m worker.recap`: the document as JSON, indented by one space), in Azure in the `cards` container. Readers of `cards` must filter on `type`.

Fields: `id`, `type`, `match_id`, `final`, `fan_team`, `analyst_team`, `texts` (`[{lang, recap, fan, analyst, claims}]`), `machine_translated`, `writer` (model name or `template`), `verification` (`{verified, attempts, report}`), `cited_cards`, `analysis`, `facts`, `field_meanings`. With `--writer foundry` the recap agent on Foundry writes it (`worker.recap.RecapWriter`), within what is left of the match's call budget, then the template; otherwise the template.

## Audit entry

One entry per step, each written once:

| Who | Entry | `action` |
|---|---|---|
| Spotter, without a desk | `{id, match_id, card_id, event_id, action, cause, message_id, minute_label}` | `hold`, `release`, `retract`, `rewrite` |
| Desk, for a card action from the feed | `{id, match_id, card_id, action, by: "feed", version, at, event_id, message_id, minute_label, cause}`, plus `was_on_air` on `retracted` | `hold`, `release`, `retracted`, `rewrite_requested` |
| Desk, for a draft or a decision | `{id, match_id, card_id, action, by, version, at}`, plus `verified` and `attempts` on `written` (the `feedback` entry is described under producer feedback) | `written`, `approved`, `edited`, `rejected`, `feedback` |
| Insight engine | `{id, match_id, card_id (the insight id), action, by: "insights", version, at}`, plus `cause`, `superseded_by` and `was_on_air` on `superseded` | `proposed`, `updated`, `revived`, `superseded` |

With a desk the Spotter's own line is not written: the desk's entry for the same card action carries its fields. `by` is a model name, `template`, `feed`, `insights`, `auto-approve` or the producer's identity. The `cause` of a superseded insight is `updated` (a new level of the fact), `goal ruled out` (a goal it counted was voided), `no longer true` (the score moved on), `correction` or `outranked` (the cap per period). With a desk, the audit entries of a card or an insight are the steps of its `history`, in the same order (a `feedback` entry leaves the judged document as it was).

## Producer decisions

`worker.desk.Desk` owns every status change: `approve(card, by, texts=None)` (with edited texts: an `edited` entry, then `approved`), `reject(card, by)`. A retracted card can never be approved. The console calls these methods; it never writes a status itself. The worker never overwrites a decision: before the feed changes a card (VAR check, correction) it reloads the stored card and keeps `status`, `texts`, `history` and `retracted_on_air` from it. Locally, where the worker rewrites the whole of `cards.json` or `insights.json` for each document, it first takes back from the file the decisions on every other document of the same version (`status`, `texts`, `history`), so writing a new card never undoes an approval made in the console.

## Feed and CDF

The feed contract is in `src/generator/feed.py`; the at-rest export in `src/generator/cdf.py` (Common Data Format 0.3.2).

## History, insights, storyline and the duo

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

A fact about the match in progress or just finished, compared with the history: `{id, type, match_id, key, kind (record, rarity, streak, milestone, storyline, tactical), minute_label, clock_s, event_id (or null), headline_facts: {field: value}, field_meanings: {field: meaning}, rarity: {p, surprisal_bits, basis}, value (0 to 100, the ranking key), status (proposed, pending, approved, rejected, superseded), texts (as cards), history}`. `rarity.basis` says what was counted ("4 goals by a substitute: 0 in 18,620 matches since 1976/77"). Every number shown comes from `headline_facts`. Insights go through the same desk and Verifier as cards. The league-wide counts, rates, ratios and bits (`rarity`, and in `headline_facts` `one_in`, `history_opportunities`, `history_per_season`, `surprisal_bits`, `times_rarer_than_runner_up`) are for the producer in the console and must not be in a text on air: on air a rarity is told in seasons, about the club or the player (the templates use `history_count`, `history_rank` and `history_seasons` for "never in 50 seasons" or "only the 6th in 50 seasons"). The desk gives no writer `surprisal_bits`, `runner_up_surprisal_bits`, `history_opportunities`, `times_rarer_than_runner_up`, `top_value` and `runner_up_value` (`worker.desk.OFF_AIR_FACTS`), so the Verifier refuses a text that quotes them. `one_in` and `history_per_season` still reach the writers, and two families of insight templates still quote them on air (see the limits in `docs/notes/insights.md`).

The engine also writes: `group` (the fact an insight updates, e.g. `scorer-home-20`) and `version`; `superseded_by` and `supersede_cause` when a newer level of the same fact, or a correction, replaces it, or when the fact is no longer true at the current score (a team that led at half-time is level again, a team that came back is behind again or pegged back to level); `under_review` while a VAR check runs on a goal the insight rests on: one it counts, or one the score it states depends on (ruled out, the goal would withdraw the insight or change its facts); `evidence` (the event ids counted); `last_time` (the most recent occurrence in the history). At full time the document `insight-<match_id>-stat-of-the-match` carries `stat_of_the_match: true`, `why_first` (bits against the runner-up, how many times rarer) and `ranked` (the top five keys with their values). Fallback texts of the main families (scorer, comeback, half-time lead lost, goals against xG) quote `headline_facts` and are claimed like card texts (`src/worker/insight_texts.py`).

### Storyline (`type: "storyline"`, id `storyline-<match_id>`)

Written before kick-off: the context of the match (table positions, stakes in % with explicit fields, form, head-to-head summary, players to watch, milestones within reach) as `facts` and `field_meanings`, plus the texts of a pre-match card.

### Commentary duo (`duo` field of cards and insights)

Two fictional commentators with complementary personalities, defined in `src/worker/duo.py` (no real person, no imitation of a real commentator). A card or insight may carry `duo: [{speaker, lang, line, claims}]`, two to four lines per language, every number claimed like `texts`. Personas: one carried by emotion and the story, one by the detail and the numbers. The lines are suggestions: text proposed to the human commentary team, never voiced by Watershed.

Speakers are `wren` (Wren Calder, emotion and story) and `joss` (Joss Ferrier, detail and numbers); lines alternate. `duo_writer` says who wrote the lines (model name or `template`; a model's verified texts can come with template lines).

### Writer and desk fields (cards)

| Field | Type | Meaning |
|---|---|---|
| `on_air_priority` | `normal` or `low` | `low`: a goal that moves its team's chance of winning by less than 2 points; its texts give no probability (milestone or story line) |
| `scorer_match_goals` | int | goals cards: the scorer's goals in the match so far, this one included, goals ruled out excluded |
| `ruled_out` | `{reason, cause, minute_label, margin_cm}` | set by the desk when VAR voids a goal card: `reason` `offside`, `handball` or another cause, `minute_label` of the decision; `margin_cm` is always null: the duo and the recap read `ruled_out`, and the margin and its call stay in the card's `var_decision`, for the producer |
| `duo_ruling` | array, as `duo` | the duo's suggested lines when VAR rules the goal out (templates only, verified) |
| `verification.repetition_warnings` | int | phrases, openings or tics already used in the match (warnings; an identical headline fails the draft) |

"Points" on screen means only a change in a team's chance of winning (`swing_pts`, `save_value_pts`, `turning_point_swing_pts`). `value_pts` ranks cards and is never written in a text.

Insights and the storyline are written by `Desk.write_insight(doc, teams)`: same Verifier, retry and template path, same statuses and audit entries as cards, stored through the sink's `insight` method when it has one, else `card`. The writer is the desk's `insight_writer`, the card writer when it is None. The worker passes the templates (`python -m worker --insights-writer template`, the default): the model writes only the cards with their duo and the recap. `--insights-writer writer` sends the insights and the storyline to the card writer too.

Model calls: at most 40 per match (`Desk.max_calls_per_match`), shared by every draft sent to a model, one call each and two when the Verifier sends the draft back; beyond the budget, the templates write. With the defaults, the demo match asks for 12 card drafts (11 cards and one rewrite) and one recap: 13 calls when every draft passes the first time, 26 at most. Each time the recap is written again (a card action after the whistle, a flush that finds a producer decision made since), it is one more draft, one or two calls, within the same budget.

The recap (`type: "recap"`) adds `analysis.disallowed_goals`: `[{card_id, minute_label, team, player, reason, margin_cm}]`, read from the `ruled_out` records of retracted goal cards (or passed to `build_recap(..., disallowed=...)`); the texts name them only as ruled out, with the reason. Its `margin_cm` is null, as in `ruled_out`: the margin, in centimetres or in words (tight, clear, by a whisker), is for the producer, in the card's `var_decision`, and is never said on air. Only a `disallowed` passed with a margin would bring the call back into the texts; the worker never passes one.

## Producer feedback (`type: "feedback"`)

A producer's thumbs up or down on one sentence of a card, an insight or the storyline, written by `Desk.feedback(doc, target, verdict, reasons, note, by, teams=None)`. Locally appended to `data/desk/<match_id>/feedback.jsonl` (a later line with the same `id` replaces an earlier one); in Azure upserted in the `cards` container next to the cards. The judged document is not rewritten: a feedback changes no status, text or history.

| Field | Type | Meaning |
|---|---|---|
| `id` | string | `feedback-<doc_id>-v<doc_version>-<part>-<lang>-<index>-<hash of by>`: one per producer, sentence and version; a second click replaces the first |
| `match_id`, `doc_id`, `doc_type`, `doc_version` | string, string, `card`/`insight`/`storyline`, int | the document judged and its version |
| `moment` | string or null | card moment (`goal`, `save`, `red_card`, `chance`) |
| `family` | string or null | insight family, the words of the key before the first id, side or number (`scorer`, `comeback`, `ht-lead-lost`), or `storyline` |
| `target` | `{part, lang, index}` | `part` `headline`, `why` or `duo`; `index` 0 for a headline or why, the position among the duo lines of that language for a duo line |
| `text`, `speaker` | string, string or null | the exact text judged, and the duo speaker |
| `writer` | string | `writer` of the document (model name or `template`), `duo_writer` for a duo line |
| `verdict` | `up` or `down` | |
| `reasons` | array | on `down` only, among `wrong_fact`, `not_relevant`, `repetitive`, `wrong_tone`, `too_long`, `wrong_moment` |
| `note` | string or null | the producer's note, 200 characters at most |
| `by` | string | the producer's identity (never exported) |
| `facts`, `field_meanings` | objects | the facts the sentence was written from (`card_facts` or `insight_facts`) |
| `pattern` | string | the sentence's skeleton (names of the facts as `{}`, numbers as `#`), to match template variants |
| `at` | number | epoch seconds |

Audit entry: `{id, match_id, card_id, action: "feedback", by, version, at, target, verdict, reasons}`. The console calls it through `Store.feedback`; like approve, it is refused without a signed-in producer in public mode.

The producer profile (written to `FEEDBACK_PROFILE`, default `data/feedback/profile.json`; the worker reads it only when `FEEDBACK_PROFILE` is set), built by `worker.feedback.build_profile` from every feedback of every match: `{version, built_at, votes, approval_rate, rules: [{rule, source, count, last_at}], examples: {rejected, approved}: [{text, part, lang, moment, family, reasons, note, at}], families and moments: {name: {up, votes, rate, weight}}, demoted: [pattern]}`. Insights whose family the feedback weighs carry `feedback_weight` (their `value` is already weighted). The dataset export (`python -m worker.feedback export`) writes `record: "judgement"` lines (the feedback without `by`) then `record: "pair"` lines `{doc_id, part, lang, facts, field_meanings, chosen, rejected, rejected_reasons, ...}`.
