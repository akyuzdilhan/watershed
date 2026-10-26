# Match graphics, tactical details, insights and storyline panels

Everything here is computed from the feed's actions and their 360 freeze frames, with corrections applied (`src/web/matchfeed.py`: `update` and `reassign` merge their changes, `void` marks the action `voided` so a goal ruled out is still drawn as such). No model call, no Azure.

## Graphics (`src/web/graphics.py`)

| Function | What it shows |
|---|---|
| `shot_map` | Every shot on one pitch, home attacking right. Marker area grows with xG (size = 8 + 34 √xG px). Outcome by colour and shape: goal (yellow, filled), goal ruled out by VAR (purple cross), saved, post, blocked, off target. Header per team: goals, shots, xG, without the shots VAR ruled out (still drawn as purple crosses), as in the insight facts: on the demo Varleigh have 13 shots for 1.28 xG (the xG fact says 1.3), not 14 for 1.54. |
| `buildup_figure` | The possession behind a goal or a big chance (xG ≥ 0.2), last 10 actions, as arrows over the players at the final action. Arrow width = threat added by the action (1.5 px for none, up to 12 px); the shot arrow's width is its xG, its colour its outcome. |
| `set_piece_figure` | A corner or free kick animated in up to three steps: the frame at the delivery, the frame at the first contact, then what the contact did. The delivery arrow's width is the value of where it landed; a contact won by the attackers is sized by its threat added, a defensive contact is thin and grey. |
| `set_pieces_map` | Every corner and crossed free kick of a team: arrow width by the value of the landing point, team colour when the attackers won the first contact, grey when they lost it. |
| `momentum_figure` | Threat added per 5 minutes per team as mirrored bars (home up, away down), goals labelled (x for ruled out), and the desk's win-probability curve in a second panel with its own axis. The second half starts after the first half's added time. The shots xG in the hover leaves out the shots VAR ruled out. |
| `shot_on_goal_figure` | A penalty or a direct free kick seen from behind the taker: where the ball crossed the goal line (`end_y`, `end_z`) and where the keeper stood in the frame. |
| `line_break_figure`, `run_figure`, `space_figure`, `line_height_figure`, `pressing_figure` | The tactical details below. `run_figure` reuses `pitch.offside_figure` with the runner; the console always draws it with `show_margin=False`, the margin goes under "Producer details". `pressing_figure` is titled "Opponents near the ball" (formerly "Pressing intensity"), for what it counts: per team, the mean number of its players within 5 m of the opponent on the ball. |
| `insight_figure(insight, feed)` | The figure an insight names in its `figure` field. |

"Threat" is `worker.tactics.location_value`: the xG of an unblocked footed shot from a location, with the generator's provider xG constants. A pass's threat added is the value where it ended minus where it started. It is a simple, explainable surface, not a fitted expected-threat grid: it is near zero outside the final third, so the momentum bars mostly measure entries into dangerous areas.

## Tactical details (`src/worker/tactics.py`)

Pure functions on actions with frames. Frames are in the acting team's frame (it attacks towards x = 105).

| Detector | Definition |
|---|---|
| `line_breaks` | Completed passes (the next action is a team-mate's, starting within 2 m of the pass end) and the outfield opponents standing strictly between the passer's x and the pass end's x. Keepers' passes are left out (a long ball from the keeper bypasses everyone by construction). This is the "packing" count, lateral position ignored. |
| `runs_behind` | Passes in the opposition half that end beyond the offside line (second-last defender, keeper included). Runner: the player named by a VAR offside update, else the team-mate nearest the end of the pass. Margin positive = beyond the line. The official VAR figure is used when the feed has one (0.25 m at 63'; the frame shows 0.20 m because locations are rounded to 0.1 m). |
| `space_at_reception` | Distance from the receiver (actor of the next action) to the nearest opponent, keeper included, in the frame of that next action. |
| `pressure` | Opponents within 5 m of the player on the ball, open play only, keepers included. The console draws it as "Opponents near the ball"; the insight candidate built on it keeps the name pressing, and without a text the console titles it "<team>: more (or fewer) players near the opponent on the ball after the break". |
| `line_heights` | Each team's deepest outfield player, in metres from its own goal line. The figure plots its mean per 5 minutes when the opponent has the ball; the insight candidate compares the medians of the two halves. |

`insight_candidates(actions, info, match_id)` builds documents per `docs/contracts.md` ("History, insights, storyline and the duo"), kind `tactical`, best first: the pass that bypassed the most defenders in the build-up of a goal (two at most), the tightest run behind the line before a goal (the run before the 63' goal comes out with a value of 88), the scorer with the most space at reception (when at least twice the match median), a defensive line that moved 3 m or more between the halves, a change of 20 % or more in pressing. Every number shown is in `headline_facts`, every field has its meaning in `field_meanings` (the function refuses a fact without one), `rarity` is measured within the match ("7 defenders bypassed: 26 of 930 completed passes in this match"), `value` (0 to 100) is a hand-set ranking, and `figure` names the figure to draw. On the demo: the 82' pass by #10 that bypassed 7 defenders before #20's fourth goal, the 63' run, the 57' pass that bypassed 6 before #20's second goal, Yarnford's 14' run 0.40 m inside the line, Yarnford's line 3.1 m deeper after the break.

The insight engine (`worker.insights`) builds the history-based insights; these candidates are for it or for the Writer to take. They carry `status: proposed` and no texts.

## Console and overlay

The console (`/`) has a view switch in its header: Desk (unchanged), Match graphics, Insights and storyline (`src/web/panels.py`).

- **Match graphics**: a kind (shot map, momentum and win probability, build-ups, corners and free kicks, penalties and direct free kicks, tactical details) and an item (which goal, which set piece, which line break, run or reception). Under tactical details: defensive line height and opponents near the ball over time, then the 8 biggest line breaks, the 8 tightest runs and the 6 freest receptions.
- **Insights and storyline**: the insight documents of the desk (`insights.json` or Cosmos docs with `type: insight`), highest value first, with texts, the duo's lines under "Suggested lines" (text proposed to the commentary team, never voiced, signed with the persona's short name), facts with their meanings, rarity basis and figure; Approve and Reject. When the desk holds no insight, the list shows the candidates computed from the feed, labelled as such, with nothing to approve. The storyline (`storyline.json` or `storyline-<match_id>`) with texts, suggested lines, facts and meanings, Approve and Reject.
- **After the match**, under the storyline: the recap, the supporters' text and the analyst text of the recap document that the worker writes at the final whistle (`Store.recap`: `recap.json` in the match folder, in the format `python -m worker.recap` writes, or `recap-<match_id>` in the `cards` container), in every language like the other texts, Spanish labelled "AI-written, not reviewed" like on the overlay, with the Verifier's verdict and the writer. Read only: nothing to approve. Before the final whistle it says "The recap appears at full time."; the recap is read only once the match state is final, so a recap left by an earlier run of the match never shows while it is played again. At full time a recap whose score or teams are not the match state's is not shown either (`same_match`): one left by an earlier run that ended otherwise, or written before a later goal, waits for the worker's new recap. A recap left by an earlier run with the same score and teams still shows for the time the worker takes to write the new one; deleting the match's documents before a replay, or a worker that writes the recap before the final state, closes that window. It comes with the insights refresh, in the same request.
- **Read-only view**: in public mode a visitor who is not signed in sees both panels, After the match included, without thumbs and with Approve and Reject disabled; a decision without a producer is refused on the server (`decide_doc`). What stays with the producer is left out on the server, not hidden in the page: the offside margin (run labels, a run candidate's title, the `margin_m` fact, and `pitch.offside_figure(show_margin=False)`: no bracket, no metres in the annotation, the title or the meta; the line, the runner and the second-last defender stay) and the counts, rates and bits behind a rarity (`PRODUCER_ONLY_FACTS`: `one_in`, `surprisal_bits`, `history_count`, `history_opportunities`, `history_per_season`, `runner_up_surprisal_bits`, `times_rarer_than_runner_up`, and the `rarity.basis` line). A line under the facts says so when some were left out. Locally with `PUBLIC_MODE=0` the local producer sees everything. The producer gets the producer-only facts and the rarity basis folded under "Producer details", closed by default, with the `margin_m` fact; run labels, insight titles and run figures never carry the margin, for the producer either, and the graphics tab folds the margin of the run picked under the note (`run_margin_details`). The list of insights shows the type of each fact in words (`web.labels.fact_label`: "Half-time lead lost", "Run behind the line"), and the facts table the fields in words with the key on hover.
- **Cards**: the selected card shows its `duo` lines when present, drawn by the same `duo_block`.
- **Decisions on insights and storyline**: `Store.decide_doc` calls the desk's `approve_insight` / `reject_insight` (or `_storyline`) if it has them, else the generic `approve` / `reject`; the local sink files the document by its `type` (insights.json, storyline.json, cards.json), the audit entry carries the document id as `card_id`. A desk with neither shows "The desk cannot decide insights or storylines yet".
- **Overlay**: an approved insight with texts can take the lower-third, tagged "The detail" (fr "Le détail", es "El detalle"), purple border. Same rule as cards: the latest approval in the audit. The overlay still imports only `web.store` (which now imports `web.matchfeed`, plain Python); a test checks it never loads `worker`, `web.graphics`, `web.panels` or `web.console`.

Where the feed comes from: locally `<DESK_DIR>/<match>/feed.jsonl[.gz]`, else `<REPLAY_DIR>/<match>.jsonl[.gz]` (`REPLAY_DIR` defaults to `data/replay`); in Azure the `events` container, read incrementally by `_ts`. While the match state is not final, the feed is cut at its `last_seq`, so a replay file does not show what the desk has not reached yet. The feed is cached 5 s per process.

Rate limit, counted per IP: the desk ticks every 1.5 s, about 80 requests a minute for its two callbacks (the match list and the desk refresh). The match list sets the match picker again on every tick, and Dash runs every callback that takes the picker as an input each time, even when its value is the same: the panels take a copy instead (`c-p-match`), written in the browser by a clientside callback (`SAME_MATCH`) only when the match changes, so the tick sends them nothing. The graphics and insights panels refresh every 5 s with their own timers, enabled only while their view is shown: 12 a minute, the After the match section included (same request as the insights). The overlay ticks every second, 60 a minute, on the same IP when it runs on the producer's machine (as for the video). Together about 152 a minute, under a `RATE_LIMIT_PER_MIN` of 600; the former 120 was passed as soon as the overlay was open next to the console. Before the panels had their own timers, four callbacks on the shared tick went past 120 and the console got 429s; before the copy, both panels still answered every tick with an empty response, about 160 a minute for the console alone.

## How to view

```
set PYTHONPATH=src
.venv\Scripts\python.exe -m worker data/replay/demo-0007.jsonl.gz --writer template --desk-dir data/desk
set DESK_DIR=data/desk
set PUBLIC_MODE=0
.venv\Scripts\python.exe -m web.app        # http://127.0.0.1:8050/, header: Match graphics, Insights and storyline
```

Standalone HTML of any figure: `graphics.shot_map(matchfeed.build(matchfeed.load(path))).write_html("shots.html", include_plotlyjs="cdn")`.

## Limits

- The frames come from the generator's `Shape`, not tracking data. The nearest defender always closes the ball down to 1.2 to 5 m when within 15 m, so there is almost always one opponent near the ball (the "Opponents near the ball" figure is flat and the pressing insight rarely fires) and space at reception has a median near 3 m. Attackers stay onside unless a run is planted. Locations are rounded to 0.1 m.
- The packing count ignores lateral distance: a defender 30 m away across the pitch counts if he is between the passer and the receiver along its length.
- Threat added uses a shot-value surface, not a possession-value model; carries count as much as passes.
- A first contact is the next action in the feed; an aerial duel or a flick-on that the feed does not record is invisible.
- The build-up title carries the feed's score at the shot, so a goal later ruled out shows the score with it.
- Insight `value` weights are hand-set; `rarity` is within the match, not against a history.
- In Cosmos the first read of a match's events reads every message (about 1,800 for a match, frames included); later reads are incremental.
- The web image ships `src/` (it needs `worker/tactics.py`) and runs in Azure.
- The read-only view is tested through real `_dash-update-component` POSTs; the clientside copy of the match picker is tested in node (`SAME_MATCH`) and in the callback graph, not in a browser.
