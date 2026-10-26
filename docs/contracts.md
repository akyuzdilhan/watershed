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
| `var_decision` | `{reason, call, margin_m}` | set by the worker when a correction voids the card (and kept on the card, so it reaches Cosmos DB): `reason` the cause of the correction (`var_offside`, `var_handball`, or another cause such as `provider_review`), `margin_m` the offside margin in metres the feed sends on the pass in a correction tied to the same VAR decision (else null), `call` `tight` or `clear` from that margin (`writer.offside_call`: tight up to 30 cm), else null. The margin is for the producer only: never shown to anonymous visitors (in public mode the console's API and panels leave `margin_m` out for them; the worker writes it on the card for the signed-in producer). The console may show the call to everyone who can see the card ("tight call"). On air, the margin is never given as a number (no centimetres, no metres); how close it was may be said in words, from `call`: tight ("by a whisker", "d'un rien", "por muy poco") or clear ("clear", "net", "claro"). The desk does not copy the margin into `ruled_out`; the duo and the recap read the call through `writer.var_ruling` |
| `texts` | array of `{headline, why, lang, claims}` | one per language (`en`, `fr`, `es`); absent before the Writer |
| `machine_translated` | array of language codes | to label on air (`es`) |
| `writer` | string | model name or `template` |
| `verification` | `{verified, attempts, attempts_log, fallback_reason}` | `attempts`: model calls made for this version; `attempts_log` and `fallback_reason`: the trace (below) |
| `writer_config` | `{deployment, effort, max_tokens, prompt_sha}` or null | the model writer called for this version (trace, below); null when no model was called |
| `history` | array of `{action, by, version}` | short form of the audit trail |
| `analysis` | object | only with `--analyst template` or `foundry` ("Analyst notes" below), on goal, save and chance cards; absent otherwise |

### Writer trace

Written by the desk on every card, insight and storyline (`verification.attempts_log`, `verification.fallback_reason`, `writer_config`), copied as is on their `written` audit entry, and written on the recap document the same way. `python -m worker.measure` reads them.

| Field | Type | Meaning |
|---|---|---|
| `attempts_log` | array of `{attempt, writer, verified, rejected, finish_reason, usage, latency_s, error}` | one entry per model call, in order (empty without a model call): `attempt` 1 or 2; `writer` the model name; `verified` whether the Verifier passed that reply; `rejected` what the Verifier refused, a list of strings, one per problem (an empty list when it passed or when the call failed; never a count); `finish_reason` as Agent Framework reports it (`stop`, `length`, `tool_calls`, `content_filter`) or null; `usage` `{input_tokens, output_tokens, reasoning_tokens}` of the whole run, tool loop included (Responses API: `output_tokens` includes `reasoning_tokens`), null values when unknown; `latency_s` the wall time of the call; `error` null, or why the call gave no usable reply: `timeout: ...`, `rate_limit (HTTP 429): ...`, `network: ...`, `server_error: ...`, `error: ...` (another exception), `truncated: finish_reason length at N output tokens` |
| `fallback_reason` | string or null | why the template wrote all or part of the document: `verifier` (the Verifier refused the last reply; the model's texts may still be kept with template duo lines), `truncated` (the last reply was cut by the token limit), `timeout`, `rate_limit`, `network`, `server_error` (no second call), `error`, `no_budget` (the match's call budget was spent), `circuit_open` (the desk paused the model after 3 drafts in a row whose calls all failed, for 5 minutes), `duo_not_supported` (a writer without duo mode); null when the model's texts and duo were kept, and when the desk's writer is the template |
| `writer_config` | `{deployment, effort, max_tokens, prompt_sha}` or null | the deployment name, the reasoning effort (null when unset), the output token budget of a first call (a call asked again after `finish_reason` `length` had `writer.LENGTH_RETRY_FACTOR`, 2, times it), and the first 12 characters of the SHA-256 of the agent's instructions (`writer.prompt_sha`) |

The model gets at most two calls per draft. A timeout (45 s per call, tool loop included), a 429, a network error or a 5xx goes straight to the template, with no second call; the OpenAI client inside Agent Framework does not retry either. Another exception gets a second call without a Verifier note. A reply cut by the token limit (`finish_reason` `length`) gets a second call with twice the output budget, then the template. Agent Framework's tool loop is bounded to 3 model round trips and 3 function calls per run (`writer.TOOL_LOOP`).

## Match state (`type: "match_state"`, id `state-<match_id>`)

`teams` (`{home, away}` names), `score`, `reds`, `final`, `period`, `clock_s`, `minute_label`, `last_seq`, and `wp_timeline`: one point per action, `{seq, event_id, clock_s, minute_label, home, draw, away}` (probabilities after the action, between 0 and 1).

In Cosmos DB the curve is thinned before each write (one point in ten, the last three, and both sides of every jump of 5 points or more), and the document is rewritten on every card or card action, at the end of each half, on a correction and every 25 actions. The local `state.json` keeps every point.

## Recap (`type: "recap"`, id `recap-<match_id>`)

The post-match recap, fan and analyst texts in each language (`en`, `fr`, `es`), the turning point and the facts they were checked against, built from approved cards only (`worker.recap.build_recap`). The worker's desk writes it at the final whistle, from the cards with the producer's decisions read back from the store, and writes it again when the feed changes a card after the whistle or when the worker flushes (a Service Bus session going quiet, the end of a replay) within an hour of the whistle (`Pipeline.recap_window_s`) and finds a card whose status or version changed since (`python -m worker ... --no-recap` turns it off; there is no recap without a desk). `python -m worker.recap` writes it again on demand from the stored cards, for instance after approvals made once the worker has stopped: from a desk directory, with the template writer, it writes the bytes the worker wrote; with `--cosmos` it reads the match state as stored, whose curve is thinned, so the curve points of the recap (half-time, low point) may differ from those the worker wrote from every point. Locally in `data/desk/<match_id>/recap.json` (the format of `python -m worker.recap`: the document as JSON, indented by one space), in Azure in the `cards` container. Readers of `cards` must filter on `type`.

Fields: `id`, `type`, `match_id`, `final`, `fan_team`, `analyst_team`, `texts` (`[{lang, recap, fan, analyst, claims}]`), `machine_translated`, `writer` (model name or `template`), `verification` (`{verified, attempts, report, attempts_log, fallback_reason}`), `writer_config` (the writer trace, as on a card), `cited_cards`, `analysis`, `facts`, `field_meanings`. With `--writer foundry` the recap agent on Foundry writes it (`worker.recap.RecapWriter`), within what is left of the match's call budget, then the template; otherwise the template.

## Audit entry

One entry per step, each written once:

| Who | Entry | `action` |
|---|---|---|
| Spotter, without a desk | `{id, match_id, card_id, event_id, action, cause, message_id, minute_label}` | `hold`, `release`, `retract`, `rewrite` |
| Desk, for a card action from the feed | `{id, match_id, card_id, action, by: "feed", version, at, event_id, message_id, minute_label, cause}`, plus `was_on_air` on `retracted` | `hold`, `release`, `retracted`, `rewrite_requested` |
| Desk, for a draft or a decision | `{id, match_id, card_id, action, by, version, at}`, plus `verified`, `attempts`, `attempts_log`, `fallback_reason` and `writer_config` on `written` (the writer trace; the `feedback` entry is described under producer feedback) | `written`, `approved`, `edited`, `rejected`, `feedback`; `model_paused` (`failures`, `seconds`, `error`) and `model_resumed` (`failures`) by `desk`, the circuit breaker |
| Desk with the Analyst (`--analyst`, see "Analyst notes") | `{id, match_id, card_id, action, by, version, at}`, plus `verified`, `attempts`, `attempts_log`, `fallback_reason`, `writer_config` on `analysis_written` (`by` the model name or `template`; `cause: retracted` when the goal was ruled out before the notes were applied), `cause` on `analysis_retracted` (`by: feed`) | `analysis_written`, `analysis_retracted` |
| Desk with the duo by agents (`--duo agents`, see "Agents and MCP") | `{id, match_id, card_id, action, by: "agents:<model>", version, at, cause}`, plus `turns`, `runs`, `ms`, `tools` on `duo_written`, `reason` and `kept` (who wrote the duo that stays: the Writer's model or `template`) on `duo_fallback` | `duo_written`, `duo_fallback` |
| Insight engine | `{id, match_id, card_id (the insight id), action, by: "insights", version, at}`, plus `cause`, `superseded_by` and `was_on_air` on `superseded` | `proposed`, `updated`, `revived`, `superseded` |

With a desk the Spotter's own line is not written: the desk's entry for the same card action carries its fields. `by` is a model name, `template`, `feed`, `insights`, `auto-approve`, `agents:<model>` or the producer's identity. The `cause` of a superseded insight is `updated` (a new level of the fact), `goal ruled out` (a goal it counted was voided), `no longer true` (the score moved on), `correction` or `outranked` (the cap per period). With a desk, the audit entries of a card or an insight are the steps of its `history`, in the same order (a `feedback` entry leaves the judged document as it was).

## Producer decisions

`worker.desk.Desk` owns every status change: `approve(card, by, texts=None)` (with edited texts: an `edited` entry, then `approved`), `reject(card, by)`. A retracted card can never be approved. The console calls these methods; it never writes a status itself. The worker never overwrites a decision: before the feed changes a card (VAR check, correction) it reloads the stored card and keeps `status`, `texts`, `history` and `retracted_on_air` from it; before the insight engine writes an insight again (a new level, a withdrawal, `under_review` set or cleared by a VAR check) it reloads the stored insight and keeps `status`, `texts` and `history` of the same version. Locally, where the worker rewrites the whole of `cards.json` or `insights.json` for each document, it first takes back from the file the decisions on every other document of the same version (`status`, `texts`, `history`), so writing a new card never undoes an approval made in the console.

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
| `ruled_out` | `{reason, cause, minute_label, margin_cm}` | set by the desk when VAR voids a goal card: `reason` `offside`, `handball` or another cause, `minute_label` of the decision; `margin_cm` is always null: the margin stays in the card's `var_decision`, for the producer; the duo and the recap read `ruled_out` and the call (tight or clear, said in words) from `var_decision` |
| `duo_ruling` | array, as `duo` | the duo's suggested lines when VAR rules the goal out (templates only, verified) |
| `verification.repetition_warnings` | int | phrases, openings or tics already used in the match (warnings; an identical headline fails the draft) |

"Points" on screen means only a change in a team's chance of winning (`swing_pts`, `save_value_pts`, `turning_point_swing_pts`). `value_pts` ranks cards and is never written in a text.

Insights and the storyline are written by `Desk.write_insight(doc, teams)`: same Verifier, retry and template path, same statuses and audit entries as cards, stored through the sink's `insight` method when it has one, else `card`. The writer is the desk's `insight_writer`, the card writer when it is None. The worker passes the templates (`python -m worker --insights-writer template`, the default): the model writes only the cards with their duo and the recap. `--insights-writer writer` sends the insights and the storyline to the card writer too.

Model calls: at most 40 per match (`Desk.max_calls_per_match`), shared by every draft sent to a model, one call each and two when the Verifier sends the draft back; beyond the budget, the templates write. With the defaults, the demo match asks for 12 card drafts (11 cards and one rewrite) and one recap: 13 calls when every draft passes the first time, 26 at most. Each time the recap is written again (a card action after the whistle, a flush that finds a producer decision made since), it is one more draft, one or two calls, within the same budget.

The recap (`type: "recap"`) adds `analysis.disallowed_goals`: `[{card_id, minute_label, team, player, reason, margin_cm}]`, read from the `ruled_out` records of retracted goal cards (or passed to `build_recap(..., disallowed=...)`); the texts name them only as ruled out, with the reason and, for an offside, how close it was in words when the card's `var_decision` has a call ("offside by a whisker", "hors-jeu d'un rien", "clear offside"). Its `margin_cm` is null, as in `ruled_out`: the margin is for the producer, in the card's `var_decision`, and is never given on air as a number.

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

## Agents and MCP (behind options)

Nothing in this section runs by default: without `--facts mcp` or `--duo agents`, the worker neither imports nor starts any of it, and its outputs are unchanged.

### The desk's MCP server (`worker.mcp_server`, server name `watershed-desk`)

Read-only tools over what the desk has written and over the league history. Every value is computed by code. A tool returns text content only (no output schema): the JSON `{"facts": {field: value}, "field_meanings": {field: meaning}, "subject": [club or player names]}`, at most 40 facts, each value a string or a number (never a bool, a list or null), or `{"error": message}`. `get_moment` returns exactly the JSON of the Writer's local `get_moment` tool (`{"facts", "field_meanings"}`, no subject). Every tool is annotated `readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false`; an argument outside its enum or over its length (in brackets) is refused before the tool runs.

| Tool | Arguments | Fields | Read-only | Transport |
|---|---|---|---|---|
| `get_moment` | `card_id` | the facts of a card, an insight or the storyline, as the Writer gets them (`card_facts`, `insight_facts`) | yes | in process (`--facts mcp`); stdio |
| `get_match_state` | `card_id` (120) | `match_minute`, `match_score_now`, `match_goal_log`, `match_<club>_goals`, `match_<club>_red_cards`, `match_<club>_largest_deficit` (goals ruled out not counted) | yes | in process (`--duo agents`); stdio |
| `get_storyline` | `match_id` (120) | `story_thread<i>_angle`, `story_<storyline field>` (no league points, no `_pts` field, no list), `history_through` | yes | in process (`--duo agents`); stdio |
| `player_history` | `name` (80), `club` (60, optional) | `player_<name>_name`, `_league_goals`, `_appearances`, `_sub_appearances`, `_first_season`, `_clubs`, `_goals_last_season`, `_best_season`, `_best_season_goals`, `history_through` | yes | in process (`--duo agents` with `--history`); stdio with `--history` |
| `club_history` | `club` (60) | `club_<club>_titles`, `_seasons_in_league`, `_best_finish`, `_founded_season`, `_last_title_season`, `_seasons_since_last_title`, `history_through` | yes | as above |
| `head_to_head` | `club_a`, `club_b` (60) | `h2h_<a>_<b>_meetings`, `_draws`, `_first_season`, `_<club>_wins`, `_last_meeting`, `_biggest_<club>_win`, `history_through`; the clubs in alphabetical order, so the text is the same both ways | yes | as above |
| `last_time` | `kind` (`hat_trick`, `hat_trick_by_substitute`, `brace_by_substitute`, `comeback_win_from_two_down`), `club` (60, optional) | `<key>_count`, `<key>_seasons_with_one`, `last_<key>_season` (or `never in the history`), `last_<key>_match`, `last_<key>_seasons_ago`, `last_<key>_club`, `last_<key>_player`, `history_seasons`, `history_since`, `history_through`; `<key>` is the kind, followed by the club's slug when one is given; `<key>_count` and the league-wide `<kind>_seasons_with_one` are context for the agents, refused on air by their Verifier | yes | as above |

Stdio: `python -m worker.mcp_server --desk-dir D [--history H]` serves the same tools for any MCP host over a folder written by the worker with `--desk-dir` (cards, insights, storyline and state are read again at each call; `get_moment` gives the same JSON as in process; a `match_id` that is not a folder right under `D` reads nothing). Nothing but the protocol goes to stdout.

The history facts stop at `history_through`: the current season and the match in progress are never in them. Rarity is told in seasons, never as a number of matches, and no value lies between 0 and 1 (the Verifier reads such a number as a misused probability).

### The duo by two agents (`--duo agents`, `worker.duo_agents`, `worker.agent_desk`)

The card is written, checked and stored as by the plain desk, with the template duo. For a card worth it (moment goal, save, red card or chance, not `on_air_priority: low`, `value_pts` at least `--duo-min-value`), the Writer writes the texts only; then Wren and Joss, two agents with their own MCP tools (Wren `get_match_state`, `get_storyline`; Joss the four history tools), speak in a running order fixed by code, each turn checked by the Verifier before the other hears it. When the exchange passes, its lines replace the template duo; the texts and the producer's decisions (`status`, `texts`, `history`, `retracted_on_air`, read back from the store) are kept. When it does not, the template duo stays. `duo_ruling` (VAR) is never written by the agents.

| Field (cards) | Type | Meaning |
|---|---|---|
| `duo_writer` | string | also `agents:<model>` (`agents:dry` offline) when the agents' duo replaced the template |
| `duo_trace` | object | `{model, plan: [[speaker, role]], turns: [{slot, speaker, role, attempts, ms, tools: [{name, args, fields, error}], rejected, verified}], runs, ms, tool_facts: {field: {value, tool, args}}, conflicts, applied, reason, repetition_warnings}`; set when the agents ran, or were skipped for the budget or a failed hand-over, for the current version only (a rewrite drops the trace of the version before). `tool_facts`: the tool facts the lines rely on (claimed, holding a name a line says, or backing a number word); `conflicts`: tool values that differed from a fact of the card or of an earlier tool result, set aside |

Re-verification: a duo written by the agents passes `verify_duo(duo, languages, card_facts ∪ {field: value of tool_facts})`, and every number of a line is claimed against those facts. On top of the Writer's duo checks, each line was refused for a capitalised word absent from the facts and tool results (Wren, Joss and VAR aside), at the start of a sentence too, where only a common word of the language may stand; a number word without a whole-number fact of that value (three, trois, tres, tercer, tercera, a number in several words read whole such as dix-sept, and the words for a player's goals in a match: hat-trick, brace, doublé, triplé, doblete, triplete, póker); at the table turn, no `season_` claim; a tool number in a sentence that does not name what the tool result is about (its `subject`; a player's surname is enough); and a claim on a tool's `_count` field, on a number of matches (`head_to_head`'s `_meetings`, `_wins`, `_draws`, `player_history`'s `_appearances` and `_sub_appearances`) or on `last_time`'s league-wide `<kind>_seasons_with_one`, which also back no number word.

Audit entries: `duo_written` (`by: agents:<model>`, with `cause` (who spoke, the tools called, runs and seconds), `turns`, `runs`, `ms`, `tools` such as `player_history(Marijn Scarpetta)`), or `duo_fallback` (`by: agents:<model>`, with `reason`, `cause` and `kept`, the `duo_writer` of the duo that stays), the reason one of `numbers`, `style`, `names`, `table`, `subject`, `format`, `final`, `budget`, `timeout`, `stale`, `cancelled`, `retracted`, `rejected`, `error:<type>`. A job for a version of the card that was rewritten since writes nothing.

Options of `python -m worker` (all off by default):

| Option | Effect |
|---|---|
| `--facts mcp` | with `--writer foundry`: `get_moment` is read through the desk's MCP server in process (`worker.mcp_writer.McpWriter`); same prompt, tool and JSON |
| `--duo agents` | the duo by Wren and Joss after the card; needs `--writer template` or `--writer foundry` |
| `--duo-dry` | with `--duo agents`: an offline model that speaks the template lines (no Azure, no cost) |
| `--duo-calls` | agent runs per match for the duo (default 36), apart from the Writer's 40; a turn or a retry is one run; a job reserves the runs of its turns when it is submitted, and is not submitted (`budget`) when they are not free |
| `--duo-min-value` | minimum `value_pts` of a card for the agents (default 10) |
| `--duo-deadline` | seconds for one exchange from its start (default 30); beyond, the template duo stays |

## Analyst notes (`card.analysis`, behind `--analyst`)

Written by `worker.analysis.AnalystDesk` after the card is stored (docs/notes/analyst.md); text for the producer and the commentators, never voiced, never approved. Without `--analyst template` or `--analyst foundry` the field is absent and nothing of it is imported.

| Field | Type | Meaning |
|---|---|---|
| `status` | `proposed` or `retracted` | `retracted` once VAR rules the goal out: the notes are withdrawn, never used |
| `retracted_cause` | string | the cause of the retraction (`var_offside`, `var_handball`...); only when retracted |
| `card_version` | int | the card version the notes were written for; a rewrite gets new notes |
| `facts_version` | int | version of `analysis_facts` (2: defenders taken out counted goal-side at the pass and behind the receiver at reception) |
| `texts` | `{lang: [{line, claims: [{value, source_field}]}]}` | 2 or 3 notes per language (`en`, `fr`, `es`), each claim a field of `facts` |
| `writer` | string | the Analyst's model name, `dry` offline, or `template` |
| `verified` | bool | the Verifier passed every note (`verify_analysis`) |
| `machine_translated` | array of language codes | `es`, as on the card |
| `facts`, `field_meanings` | object | the facts the notes were checked against and the meaning of each field |
| `verification` | `{attempts, attempts_log, fallback_reason, report}` | the writer trace, as on a card; `fallback_reason` also `budget` (the Analyst's calls per match, `--analyst-calls`, were spent) or `error:<type>` (the job failed) |
| `writer_config` | `{deployment, effort, max_tokens, prompt_sha}` or null | as on a card; null without a model call |

The Analyst's tool, `get_analysis_facts(card_id)`, returns `{"facts", "field_meanings"}` as JSON, like `get_moment`, or `{"error": message}`.
