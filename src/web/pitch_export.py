"""Export the pitch visuals of a moment for the video: standalone HTML, and PNG when kaleido is installed.

Usage (from the repository root):
    python -m web.pitch_export data/replay/demo-0007.jsonl.gz --moment goal-63 --out docs/img/

`src` must be on the path (PYTHONPATH=src, or run from `src`). The ground truth is read next to the replay (`<match>.truth.json`) unless `--truth` is given. Writes `<moment>-offside.html` and `<moment>-offside-zoom.html` when the moment has an offside pass, and `<moment>-buildup.html` (an animation of the actions before the moment). The HTML files load plotly.js from its CDN to stay small; `--inline-js` embeds it (about 4.5 MB per file) for offline viewing.
"""

import argparse
import gzip
import importlib.util
import json
import sys
from pathlib import Path

try:
    from web.pitch import buildup, offside_figure, sequence_figure
except ImportError:  # run from src/web, as in the web container
    from pitch import buildup, offside_figure, sequence_figure


def load(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def match_actions(messages: list[dict]) -> tuple[list[dict], dict]:
    """The actions of the feed, deduplicated, with `update` corrections applied (the VAR correction adds the offside to the pass), and match_info."""
    info = next(m for m in messages if m["type"] == "match_info")
    actions: dict[str, dict] = {}
    for m in sorted(messages, key=lambda m: m["seq"]):
        if m["type"] == "action":
            actions.setdefault(m["event_id"], dict(m))
        elif m["type"] == "correction" and m["op"] == "update" and m["target_event_id"] in actions:
            actions[m["target_event_id"]].update(m["changes"], version=m["new_version"])
    return sorted(actions.values(), key=lambda a: a["seq"]), info


def player_names(info: dict) -> dict:
    return {p["id"]: f"#{p['shirt']} {p['name']}" for team in info["teams"].values() for p in team["starting"] + team["bench"]}


def export(replay: Path, moment_key: str, out: Path, truth_path: Path | None = None, theme: str = "dark", inline_js: bool = False, show_margin: bool = True) -> list[Path]:
    """`show_margin=False` draws the offside figures without the margin, for anything public (docs/img)."""
    truth_path = truth_path or replay.with_name(replay.name.split(".")[0] + ".truth.json")
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    moment = next((m for m in truth["moments"] if m["key"] == moment_key), None)
    if moment is None:
        raise SystemExit(f"unknown moment {moment_key}; known: {', '.join(m['key'] for m in truth['moments'])}")
    actions, info = match_actions(load(replay))
    home, away = info["teams"]["home"]["name"], info["teams"]["away"]["name"]
    names = player_names(info)
    by_id = {a["event_id"]: a for a in actions}
    out.mkdir(parents=True, exist_ok=True)
    figures = {}
    offside_ids = [o["event_id"] for o in truth.get("offsides", []) if o["event_id"] in moment["affected_event_ids"]]
    if offside_ids:
        pass_action = by_id[offside_ids[0]]
        figures[f"{moment_key}-offside"] = offside_figure(pass_action, True, home, away, theme, names=names, show_margin=show_margin)
        figures[f"{moment_key}-offside-zoom"] = offside_figure(pass_action, True, home, away, theme, zoom=7, names=names, show_margin=show_margin)
    figures[f"{moment_key}-buildup"] = sequence_figure(buildup(actions, moment["event_id"]), home, away, theme, names=names)
    can_png = importlib.util.find_spec("kaleido") is not None
    written = []
    for name, fig in figures.items():
        html = out / f"{name}.html"
        fig.write_html(html, include_plotlyjs=True if inline_js else "cdn", full_html=True, auto_play=False, config={"displayModeBar": False})
        written.append(html)
        if can_png and not name.endswith("buildup"):
            png = out / f"{name}.png"
            fig.write_image(png, scale=2)
            written.append(png)
    if not can_png:
        print("kaleido is not installed: HTML only", file=sys.stderr)
    return written


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m web.pitch_export", description=__doc__.splitlines()[0])
    ap.add_argument("replay", type=Path)
    ap.add_argument("--moment", required=True, help="a moment key of the ground truth, e.g. goal-63")
    ap.add_argument("--out", type=Path, default=Path("docs/img"))
    ap.add_argument("--truth", type=Path)
    ap.add_argument("--theme", choices=["dark", "light"], default="dark")
    ap.add_argument("--inline-js", action="store_true", help="embed plotly.js instead of loading it from the CDN")
    ap.add_argument("--no-margin", action="store_true", help="leave the offside margin out of the figures (for a public page)")
    args = ap.parse_args(argv)
    for path in export(args.replay, args.moment, args.out, args.truth, args.theme, args.inline_js, show_margin=not args.no_margin):
        print(path)


if __name__ == "__main__":
    main()
