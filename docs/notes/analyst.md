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
| `regain_how`, `ball_winner_name`, `regain_from_own_goal_m`, `regain_to_opponent_goal_m`, `high_regain` | the first action of the possession: a tackle, an interception, a keeper's catch, a restart, or a loose ball after the opponent's clearance or misplaced pass; its location in the attacking team's frame; high within 40 m of the opponent's goal |
| `passes_in_move`, `carries_in_move`, `players_in_move`, `move_duration_s`, `move_tempo` | the possession up to the shot; `move_tempo` quick counter (15 s or less and 6 passes or fewer), patient build-up (40 s or more), else direct move |
| `key_pass_defenders_taken_out`, `key_passer_name`, `key_pass_receiver_name`, `key_pass_length_m` | `defenders_taken_out`: for a pass gaining 10 m or more, the outfield opponents goal-side of the ball when it was played and behind the receiver when it arrived; stricter than `tactics.line_breaks` (everyone between the two points), which gave 4 to 7 on every demo goal. Demo goals: 14' 2, 31' 2, 52' 3, 57' 3, 63' 2, 66' 3, 69' 2, 83' 4, 88' 3; written only from 3 |
| `shooter_name`, `shooter_space_m`, `typical_space_m`, `space_vs_typical` (more room than usual, less room than usual, about usual: a 15% band) | `tactics.space_at_reception`: the shooter's nearest opponent when the ball reached him, against the median of every reception so far |
| `xg_pct` | the shot's xG |
| `opponent_line_before_shot_m`, `opponent_line_usual_m`, `opponent_line_gap_m`, `line_vs_average` (higher than usual, deeper than usual, as usual: a 3 m band) | `tactics.line_heights`: median distance of the defending team's deepest outfield player from its goal line while the attacking team had the ball, in the 5 minutes before the shot and over the match so far |
| `team_threat_share_before_shot_pct`, `team_threat_share_earlier_pct`, `team_threat_share_shift_pts`, `threat_share_reading` (in control from 55%, under the cosh up to 45%, else even), `threat_trend` (rising or falling by 10 points or more, else steady) | `tactics.threat_added` (forward value only) of both teams in the 10 minutes before the shot and the 10 before that: the attacking team's share; only from the 20th minute |

## Notes, Verifier, fallback

The template (`template_analysis`) keeps the two strongest notes true for the facts (`note_scores`: a high regain, a counter from deep, a quick break or patient build-up, a share of the threat that moved by 15 points or more, a line 4 m or more off its average, a pass that took out 3 defenders or more, space twice the median), the same ones in en, fr and es. Each note is one sentence that says what happened and why it matters, with at most 2 numbers. On the demo, the 52' goal:

- en: "Varleigh were in control in the last ten minutes, with 63% of the threat after 28% before, so this had been coming." "A quick counter from deep: Brett Rivero's tackle 19 m from the Varleigh goal, and 12 s later the shot was away."
- fr: "Varleigh avait la main sur les dix dernières minutes, avec 63 % de la menace après 28 % auparavant : ce but se préparait." "Un contre éclair parti de loin : le tacle de Brett Rivero à 19 m de son propre but, et 12 s plus tard le tir est parti."
- es: "El Varleigh mandaba en los últimos diez minutos, con el 63% de la amenaza tras el 28% anterior: este gol se veía venir." "Un contragolpe rápido desde atrás: la entrada de Brett Rivero a 19 m de su propia portería, y 12 s después llega el tiro."

The readings are computed in code and given to the model, which must use them instead of judging a number itself; the Verifier checks that the text follows them (`meaning_issues`): a comparison word (more, above, bigger, higher; less, below, deeper; plus, moins; más, menos) must go the way the two numbers of a pair go (the shooter's space and the usual one, the line before the shot and its average, the two threat shares), a team under the cosh is never in control and a team in control never scores against the run of play, only the shooter shoots, finishes or scores in a sentence, quick, rapid or counter only for a quick counter, no space before % in English, and at most 3 sentences and 60 words per language. The notes are one sentence each.

No fact names a word the Verifier refuses: no minute (the console shows it), no `median` or `recent` in a field name or a meaning (`typical_space_m`, `opponent_line_before_shot_m`, `opponent_line_usual_m`), the windows in words ("in the last ten minutes"), never as a number to claim.

The model replies `{"en": [{"line", "claims"}], "fr": [...], "es": [...]}`. `verify_analysis` checks 2 or 3 notes per language and each note as a duo line: every number claimed against its field (`writer.check_numbers`), `%` only from `_pct` fields, the style and on-air rules of `writer.style_issues` (no league points verb with "points", no league-wide rate, ratio, bits or count of matches, "points" only from a `_pts` field, no "swing" or "momentum" in French and Spanish, no article before a club in French, no em dash), no capitalised word outside the facts (`names_ok`), and its own rules: never an offside margin, no centimetres at all, no numbers in brackets (`data_dump`), no word of a field name (`field_word`: median, recent, "change 3", a name with an underscore), at most 2 numbers in a sentence (`too_many_numbers`). The instructions ask for a pundit's sentences, with a good and a bad example. The instructions spell out every rule with the forbidden forms, reuse the Writer's French and Spanish rules on clubs word for word, and end with a compliant reply for the 52' goal (`ANALYST_EXAMPLE`, which a test runs through the Verifier). A failed reply gets one retry whose note lists every problem the Verifier found, line by line, then what each refused rule means (`retry_note`, `RULE_HINTS`); after it, the template; a timeout, a 429, a network error or a 5xx goes to the template at once (`writer.model_attempts`).

## On the desk

`AnalystDesk` wraps `Desk` or `AgentDesk`; its own attributes all start with `analyst` so that none hides one of the desk's (`calls`, `runner`, `live` stay the desk's). The card is written and stored exactly as before; the analysis is then attached as `analysis` (docs/contracts.md), the card stored again, and an `analysis_written` entry audited with the trace. With a model, the job runs on the `analyst` thread, one at a time, and is applied at the pipeline's next message after the producer's decisions are read back from the store; a result for an older version of the card is dropped (the rewrite has its own). When VAR rules a goal out, the analysis is marked `status: retracted` with the cause and an `analysis_retracted` entry; an analysis still running when the goal is ruled out is marked when it is applied. The console shows it as withdrawn.

## Console

`web.analysis_view.analysis_block(card, lang)`: "Analyst notes", the lines in the language chosen (English when it is missing), "Proposed text, never voiced", who wrote them and whether they passed the Verifier, and, folded, "Where each number comes from" (value, field, meaning, note). Not wired into `console.py` yet.

## Running it

    python -m worker data/replay/demo-0007.jsonl.gz --writer template --analyst foundry --analyst-dry --desk-dir data/desk     # offline, the whole model path
    python -m worker data/replay/demo-0007.jsonl.gz --writer template --analyst foundry --analyst-calls 2 --desk-dir data/desk  # the model on one or two cards

Each card prints `analyst <writer>, verified <bool>, <status>: <the English notes>`, and the run ends with `analyst (<model>): N by the model, M template, K of 12 calls`.

## Limits

- Budget: the jobs run one at a time and a call counts when it is made, so each job starts with what the jobs before really spent; a job whose card was rewritten meanwhile makes no call (`superseded`). Before that change a job held two calls for its retry from the start.
- Second real run (`--analyst-calls 12`): 1 card by the model, 4 refused twice (3 or 4 numbers in a sentence, the minute claimed, "median" named after the field, "le Yarnford"), the last 5 out of budget; the facts, the instructions, the retry note and the budget were changed after it.
- First real run (26/10, gpt-5.4-nano, effort low, `--analyst-calls 2`): the 14' goal verified at the first call in 28.5 s; the other cards took the template with `fallback_reason: budget`, because a replay sends every card within seconds and the first job had reserved its retry. Its text then read like a data dump ("(34 m recent, 31 m match, change 3)"), which the rules above now refuse.
- The frames come from the generator, not from tracking: even with the stricter count, the shape is regular and the nearest defender always closes down.
- The threat share is a simple surface (the xG of a shot from a location), not a fitted expected-threat model.
- A right number can still carry a wrong reading ("had taken over" for a share that moved from 28% to 63%); the notes are for the control room, and a human reads them before anything is said.
