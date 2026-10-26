# The Analyst: tactical notes on a goal or a big chance (`worker.analysis`)

A third agent next to the Writer and the recap agent. For each goal, chance or save card, it writes 2 or 3 short notes a pundit or a coach would make, on what the replay does not show at once: where and how the ball was won, how many defenders a pass took out, the shooter's space, the defending line before the shot, which team had the threat. Text for the producer and the commentators, shown in the console, never voiced. Off by default.

## Options

| Option | What changes | Model calls |
|---|---|---|
| `--analyst off` (default) | nothing: same outputs byte for byte, `worker.analysis` is not imported | none |
| `--analyst template` | `AnalystDesk` around the desk; the template notes are attached to the card right after it is stored | none |
| `--analyst foundry` | the Analyst agent (`AnalystWriter`, Agent Framework, Responses API, tool `get_analysis_facts`) on a thread of its own (`AnalystRunner`), applied at the pipeline's next message | 1 per card, 2 with the retry |
| `--analyst-dry` | with `foundry`: a scripted agent that answers with the template, so the model path runs offline | none |
| `--analyst-calls N` (12) | the Analyst's model calls per match, apart from the Writer's 40 and the duo's 36; beyond, the template (`fallback_reason: budget`) | |

It needs `--writer template` or `--writer foundry`, and only the `agents` extra, which the worker image installs: in Azure it can be turned on with `WORKER_EXTRA_ARGS="--analyst foundry"`. Model, effort and output budget: `FOUNDRY_ANALYST_MODEL`, `FOUNDRY_ANALYST_REASONING_EFFORT`, `FOUNDRY_ANALYST_MAX_TOKENS`, else the `FOUNDRY_` ones. Credential: managed identity in Azure, `az login` locally (`writer.foundry_client`), never a key.

## Facts (`analysis_facts`)

Computed by code on the possession that ended in the shot (`tactics.chain`), from the actions before the shot only, as the desk sees them live (`feed_actions(match)`: `Match.versions` in provider order, voided ones marked). Each fact has a meaning in English, as the card facts do.

| Facts | How |
|---|---|
| `regain_how`, `regain_player`, `regain_from_own_goal_m`, `regain_to_opponent_goal_m`, `regain_zone`, `high_regain` | the first action of the possession: a tackle, an interception, a keeper's catch, a restart, or a loose ball after the opponent's clearance or misplaced pass; its location in the attacking team's frame; high within 40 m of the opponent's goal |
| `passes_in_move`, `carries_in_move`, `players_in_move`, `move_duration_s` | the possession up to the shot |
| `best_pass_defenders_bypassed`, `best_pass_passer`, `best_pass_receiver`, `best_pass_length_m` | `tactics.line_breaks` on the move: the pass that had the most outfield opponents between passer and receiver along the pitch |
| `shooter_space_m`, `match_median_space_m` | `tactics.space_at_reception`: the shooter's nearest opponent when the ball reached him, against the median of every reception so far |
| `xg_pct` | the shot's xG |
| `opponent_line_recent_m`, `opponent_line_match_m`, `opponent_line_change_m`, `opponent_line_moved`, `line_window_min` | `tactics.line_heights`: median distance of the defending team's deepest outfield player from its goal line while the attacking team had the ball, in the 5 minutes before the shot and over the match so far |
| `team_threat_share_recent_pct`, `team_threat_share_before_pct`, `team_threat_share_change_pts`, `team_threat_trend`, `threat_window_min` | `tactics.threat_added` (forward value only) of both teams in the 10 minutes before the shot and the 10 before that: the attacking team's share; only from the 20th minute |

## Notes, Verifier, fallback

The template (`template_analysis`) keeps the two strongest notes true for the facts (`note_scores`: a high regain, a counter from deep, a share of the threat that moved by 15 points or more, a line 3 m or more off its average, a pass that took out 3 defenders or more, space twice the median), the same ones in en, fr and es. On the demo, the 52' goal:

- en: "Varleigh had taken over: 63% of the threat in the 10 minutes before the shot, 35 points more than in the 10 before." "It started deep: Brett Rivero's tackle, 19 m from the Varleigh goal, then 5 passes in 12 s to the shot."
- fr: "Varleigh avait la main : 63 % de la menace sur les 10 minutes avant le tir, 35 points de plus que sur les 10 d'avant." "Tout part de loin : le tacle de Brett Rivero, à 19 m de son propre but, puis 5 passes en 12 s jusqu'au tir."
- es: "El Varleigh se había adueñado del partido: el 63% de la amenaza en los 10 minutos previos al tiro, 35 puntos más que en los 10 anteriores." "Todo empieza atrás: la entrada de Brett Rivero, a 19 m de su propia portería, y luego 5 pases en 12 s hasta el tiro."

The model replies `{"en": [{"line", "claims"}], "fr": [...], "es": [...]}`. `verify_analysis` checks 2 or 3 notes per language and each note as a duo line: every number claimed against its field (`writer.check_numbers`), `%` only from `_pct` fields, the style and on-air rules of `writer.style_issues` (no league points verb with "points", no league-wide rate, ratio, bits or count of matches, "points" only from a `_pts` field, no "swing" or "momentum" in French and Spanish, no article before a club in French, no em dash), no capitalised word outside the facts (`names_ok`), and its own rule: never an offside margin, no centimetres at all. A failed reply gets one retry with the list of what was refused, then the template; a timeout, a 429, a network error or a 5xx goes to the template at once (`writer.model_attempts`).

## On the desk

`AnalystDesk` wraps `Desk` or `AgentDesk`; its own attributes all start with `analyst` so that none hides one of the desk's (`calls`, `runner`, `live` stay the desk's). The card is written and stored exactly as before; the analysis is then attached as `analysis` (docs/contracts.md), the card stored again, and an `analysis_written` entry audited with the trace. With a model, the job runs on the `analyst` thread, one at a time, and is applied at the pipeline's next message after the producer's decisions are read back from the store; a result for an older version of the card is dropped (the rewrite has its own). When VAR rules a goal out, the analysis is marked `status: retracted` with the cause and an `analysis_retracted` entry; an analysis still running when the goal is ruled out is marked when it is applied. The console shows it as withdrawn.

## Console

`web.analysis_view.analysis_block(card, lang)`: "Analyst notes", the lines in the language chosen (English when it is missing), "Proposed text, never voiced", who wrote them and whether they passed the Verifier, and, folded, "Where each number comes from" (value, field, meaning, note). Not wired into `console.py` yet.

## Running it

    python -m worker data/replay/demo-0007.jsonl.gz --writer template --analyst foundry --analyst-dry --desk-dir data/desk     # offline, the whole model path
    python -m worker data/replay/demo-0007.jsonl.gz --writer template --analyst foundry --analyst-calls 2 --desk-dir data/desk  # the model on one or two cards

Each card prints `analyst <writer>, verified <bool>, <status>: <the English notes>`, and the run ends with `analyst (<model>): N by the model, M template, K of 12 calls`.

## Limits

- Never run against the real model yet: the fallback rate and the latency are unknown. One call per card with `get_analysis_facts`, so about the Writer's 20 to 30 s, off the card's path.
- The frames come from the generator, not from tracking: the line-break count is high on most goals (the nearest defender always closes down, the shape is regular), so "took 5 defenders out" is less rare here than in a real match.
- The threat share is a simple surface (the xG of a shot from a location), not a fitted expected-threat model.
- A right number can still carry a wrong reading ("had taken over" for a share that moved from 28% to 63%); the notes are for the control room, and a human reads them before anything is said.
