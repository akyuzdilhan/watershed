# Pitch visuals

`src/web/pitch.py` draws the freeze frames of the feed with plotly (installed with Dash, no other dependency). It does not import the generator, so it runs in the web container, which only ships `src/web`.

| Function | Use |
|---|---|
| `pitch_figure(theme, width, x_range, y_range)` | Empty 105 x 68 m pitch, `dark` or `light`, 1:1 aspect ratio (the height follows the width) |
| `frame_figure(action, home_name, away_name, theme, view, names)` | The 22 players, keepers as squares, actor ringed, shirt numbers, ball at the start, arrow to the end, minute and score in the title |
| `frame_traces(action, theme, view, names)` | The same as five traces in a fixed order (home, away, actor ring, path, ball), for the console's own layouts |
| `offside_geometry(pass_action, runner_id)` | Line at the second-last defender (keeper included, as `Shape.line`), runner, margins |
| `offside_figure(pass_action, defending_line, ..., zoom)` | The pass frame with the offside line, the runner and the second-last defender ringed, the margin bracketed and annotated; `zoom=7` gives a 14 m window for the video |
| `buildup(actions, event_id)` | The 5 to 8 actions that lead to an event (its possession, extended back if shorter) |
| `sequence_frames(actions, ...)` / `sequence_figure(actions, ...)` | Plotly animation frames of a build-up, and a figure with play, pause and a slider |

Frame of reference: the feed gives locations from the acting team, which attacks towards x = 105. `view=None` draws them as they are; `view="home"` or `"away"` flips the frame so that this team attacks towards x = 105. A build-up is drawn from the team of its last action, so that a change of possession does not flip the picture, and players carry `ids` so that plotly moves each one between frames.

The offside margin. The annotation shows the feed's figure when the action carries one (the `offside` field added by the VAR `update` correction: 0.25 m at 63'), otherwise the margin measured on the frame. The frame rounds locations to 0.1 m, so the two can differ: at 63' the frame shows the runner at 91.8 m and the second-last defender (United #6) at 91.6 m, that is 0.20 m. The line is drawn where the frame puts it; the tests check that the annotated margin is within 0.05 m of the frame geometry. The ball is drawn 1 m from the actor along the action so that it does not hide the shirt number; the true start is kept in the ball trace's `customdata`.

## Export for the video

```
set PYTHONPATH=src
python -m web.pitch_export data/replay/demo-0007.jsonl.gz --moment goal-63 --out docs/img/
```

It writes `goal-63-offside.html`, `goal-63-offside-zoom.html` and `goal-63-buildup.html`, and a PNG of the offside figures when kaleido is installed (it is not: HTML only). The HTML loads plotly.js from its CDN, which keeps each file under 40 kB; `--inline-js` embeds it for offline use (about 4.5 MB per file). `--theme light` is available.

## Notes for the console

- `src/web/__init__.py` did not exist; an empty one was added so that `web.pitch` imports with pytest's `pythonpath = ["src"]`. `web` is not in the `packages.find` list of `pyproject.toml`, so the export needs `PYTHONPATH=src`.
- In the container the working directory is `src/web`: import with `from pitch import ...` there, or fall back as `pitch_export.py` does.
- `dash==4.4.1` (from `src/web/requirements.txt`) was installed in the shared `.venv` to get plotly; `tests/test_pitch.py` skips if plotly is missing.
