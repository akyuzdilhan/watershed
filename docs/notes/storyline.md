# Storyline: what is at stake, before, during and after the match

Every match has its story; the desk's job is to find it. `src/worker/storyline.py` builds the storyline document of the contract (`type: "storyline"`, id `storyline-<match_id>`, see `docs/contracts.md`, Lot 2) from the league season (`league.season`) and, when given, the fictional history (`league.history.History`, read API only). Tests in `tests/test_storyline.py` (12 tests, under a second) use a small fake history with the contract's methods.

```
python -m worker.storyline --league-seed 9 --matchday 30 [--history data/history/<seed>] [--sims 4000] --out data/desk/demo-0007/storyline.json
```

## Pre-match: `build_storyline(seed, matchday, history=None, match_id)`

Every number is in `facts` under an explicit name, with its meaning in `field_meanings` (same convention as `writer.card_facts`: `_pct` a percentage, `_pts` percentage points, plain integers otherwise).

| Group | Fields (`{s}` is the club slug, `{e}` title, top4 or relegation) | Source |
|---|---|---|
| Table | `{s}_rank`, `_points`, `_played`, `_goal_difference`, `_points_behind_leader`, `_points_clear_of_drop_zone`, `_points_if_win`, `_points_if_draw`, `{s}_rank_if_win`, `_if_draw`, `_if_loss`, `points_gap`, `points_leader` | `season_state` after matchday 29; rank if win/draw/loss adds this result only |
| Stakes | `{s}_{e}_chance_pct`, `_chance_if_win_pct`, `_chance_if_draw_pct`, `_chance_if_loss_pct`, `{s}_{e}_stake_pts` | `match_importance` for win and loss; draw and "now" with the same generator (below) |
| Form | `{s}_form` ("WWWLD", oldest first), `{s}_form_points`, `{s}_matches_without_win`, `{s}_matches_unbeaten`, `reverse_fixture` | the season as played |
| History | `history_through`, `{s}_titles`, `_seasons_in_league`, `_best_finish`, `h2h_meetings`, `h2h_{s}_wins`, `h2h_draws`, `h2h_first_season`, `h2h_last_meeting`, `h2h_last_meetings`, `h2h_biggest_{s}_win` | `club`, `head_to_head` |
| Players to watch | `{s}_player{n}_name`, `_goals_last_season`, `_career_goals`, `_appearances`, `_milestone` ("100 goals"), `_milestone_needed` | `seasons`, `matches`, `player` |

`focus` gives, per club, the event with the largest stake (both `relegation` on seed 9). Structured copies for the console: `form`, `head_to_head`, `players_to_watch`.

`threads`: three to five reasons why the match matters, strongest first, each `{key, kind, value (0 to 100), angle, facts, field_meanings}`. `facts` is a subset of the storyline's facts (tested); `angle` is a sentence with no number, for the producer and the commentators. Candidates: the six-pointer (or one stake per club), the table (does a result move a club across the drop or top-four line), form, the reverse fixture, the head-to-head, former champions in a relegation fight, a milestone within reach (every 50 goals, every 100 appearances), a top scorer.

`texts`: a pre-match card in en, fr, es (template: the club with the most at stake, its chance now, if it wins, if it loses), checked by `writer.verify`; `verification.verified` is true on seed 9.

Seed 9, matchday 30, without history: Varleigh 17th (29 points), Yarnford 18th (28). Relegation: Varleigh 11% now, 5% if they win, 13% if draw, 22% if they lose; Yarnford 34%, 16%, 32%, 43%. Threads: relegation six-pointer (77), table "Varleigh and Yarnford can swap sides" (45), form "Varleigh much stronger over the last five" (36), reverse fixture (15).

## Live: `live_context(storyline, match_state) -> (facts, field_meanings)`

For the score in `match_state["score"]` (the contract's match state, or any `{score: {home, away}, final}`): `score_now`, and per club `{s}_rank_at_kickoff`, `{s}_rank_if_score_holds`, `{s}_points_if_score_holds`, `{s}_in_{zone}_at_kickoff` and `_if_score_holds` (zone: drop_zone, top4, first_place, from `focus`), `{s}_leaves_drop_zone_if_score_holds` or `{s}_enters_drop_zone_if_score_holds` when that changes, `{s}_{e}_chance_at_kickoff_pct`, `{s}_{e}_chance_if_score_holds_pct`, `{s}_{e}_change_since_kickoff_pts` (no sign) with `{s}_{e}_change_direction` (up, down, unchanged), and, before full time, `{s}_{e}_chance_if_{club}_score_next_pct` for each side. At 0-1 on seed 9: Yarnford 15th and out of the drop zone, relegation 34% to 16%; Varleigh 18th, 11% to 22%.

Method: the rest of the season is played `n_sims` times once per score, with the fixture fixed at that score and the generator of `match_importance` (`league.rng(matchday - 1, home, away)`). Same other results for every score, so a better score never gives a worse chance (tested on 2-0, 1-0, 0-0, 0-1), and 1-0 and 0-1 reproduce `match_importance` exactly. Each score costs about 80 ms at 4,000 runs and is cached per league object, so a match touches about ten scores. "If the score holds" adds this result to the table at kick-off: the other fixtures of the matchday are not counted (the field meanings say so).

## Post-match: `post_match(storyline, home_goals, away_goals) -> (facts, field_meanings)`

`final_score`, and per club rank, points, zone and focus chance before and after (`{s}_{e}_chance_before_pct`, `_after_pct`, `_change_pts`, `_change_direction`), plus `{s}_rank_after_matchday` (the whole matchday, other results as played, via `League.with_result`). The chance after equals the live value at full time. Seed 9, 5-2: Varleigh 17th to 14th (16th after the matchday), relegation 11% to 4%; Yarnford stay 18th, 34% to 45%.

## Integration (other owners)

- **Pipeline**: build the storyline once before kick-off (or load `storyline.json`), write it to the desk next to `state.json` (and to the `cards` container in Azure, `type: "storyline"`). Keep the document in memory and pass it with the match state to the Writer.
- **Writer**: in `card_facts`, merge `live_context(storyline, state)` for goal and red-card cards with the score after the moment (`score_before` plus the goal), and for save and chance cards also with the score had it gone in (prefix those fields, for instance `if_goal_`). The meanings come with the facts. Two Verifier points: league points such as "29 points" trip `points_not_from_pts_field` (the rule reads every "N points" as percentage points), so either the rule learns league-points fields (`_points`, `_points_if_score_holds`) or the Writer instructions say "on 29" without the word; booleans (`_in_drop_zone_*`) are not numbers and pass.
- **Recap**: merge `post_match(storyline, final score)` into the recap facts.
- **Console**: show `threads` (angle and facts) and the pre-match card before kick-off; the card goes through the desk like any other.
- **Insights**: the `milestone` and `streak` threads are natural pre-match insights (`kind` matches the insight kinds).

## What is imperfect

- The history's record shape for scorers is not in the contract: `_scorers` reads `scorers` (or `goals`) with `player_id` (or `id`) and `club` (or `team`, home or away). Check it against the real `History` once it lands, and the constructor `load_history` uses (`History.load(path)`, else `History(path)`).
- The current season is score level: no player data. Players to watch and milestones count up to the last season of the history (`history_through`), and the meanings say so; a player may already have reached his milestone in this season's 29 matchdays.
- "Now" is computed from the same simulations as the stakes, not from `season_state` (another generator), so it can differ by a point from `league.demo_fixture`'s `*_chance_pct`. Choose one before both appear on air.
- Monte Carlo noise: with 4,000 runs a chance near 30% has a standard error under a point; with `--sims 1000` (tests) about 1.5 points.
- Thread values are hand-set weights, not measured interest.
