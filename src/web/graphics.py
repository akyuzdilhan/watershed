"""Match graphics for the console and the video, drawn with plotly from the feed's actions and freeze frames (corrections applied, see `web.matchfeed`).

| Function | What it shows |
|---|---|
| `shot_map` | every shot, sized by xG, coloured and shaped by outcome; goals ruled out by VAR drawn apart and left out of the shots and xG totals; the home team attacks right |
| `buildup_figure` | the possession behind a goal or a big chance as arrows whose width is the threat each action added |
| `set_piece_figure` | a corner or free kick animated: the frame at the delivery, then at the first contact, with variable-width arrows |
| `set_pieces_map` | every corner and crossed free kick of a team: delivery arrows sized by the value of where they landed, coloured by who won the first contact |
| `momentum_figure` | threat added per 5 minutes per team (mirrored bars), and the win-probability curve below it on its own axis |
| `shot_on_goal_figure` | a penalty or a direct free kick seen from behind the taker: where the ball crossed the goal line and where the keeper stood |
| `line_break_figure`, `run_figure`, `space_figure`, `line_height_figure`, `pressing_figure` | the tactical details of `worker.tactics` |
| `insight_figure` | the figure named by an insight document's `figure` reference |

"Threat" is `worker.tactics.location_value`: the xG of an unblocked footed shot from a location, so a pass's threat added is the value of where it ended minus where it started. It is a simple, explainable surface, not a fitted expected-threat grid.
"""

import math

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from web import pitch
from web.matchfeed import player_names, team_names
from worker import tactics as T

OUTCOMES = {  # label, colour, symbol, filled
    "goal": ("Goal", "#fde047", "circle", True),
    "voided": ("Goal ruled out (VAR)", "#a78bfa", "x-thin-open", False),
    "saved": ("Saved", "#e6ebf0", "circle-open", False),
    "post": ("Post", "#e6ebf0", "diamond-open", False),
    "blocked": ("Blocked", "#94a3b8", "square-open", False),
    "off_target": ("Off target", "#64748b", "circle-open", False),
}
BIG_CHANCE_XG = 0.2
SET_PIECES = {"corner_crossed", "corner_short", "freekick_crossed", "freekick_short"}
DIRECT = {"shot_penalty", "shot_freekick"}
GOAL_W, GOAL_H = 7.32, 2.44


def _t(theme):
    return pitch._theme(theme)


def _to_home(a: dict, x: float, y: float) -> tuple[float, float]:
    """A point of `a`'s frame drawn with the home team attacking towards x = 105."""
    return (x, y) if a["team"] == "home" else (pitch.LENGTH - x, pitch.WIDTH - y)


def _outcome(a: dict) -> str:
    o = (a.get("shot") or {}).get("outcome", "off_target")
    if o == "goal" and a.get("voided"):
        return "voided"
    return o if o in OUTCOMES else "off_target"


def _who(a: dict) -> str:
    p = a.get("player") or {}
    return f"#{p.get('shirt', '')} {p.get('name', '')}".strip()


def _arrow(sx, sy, ex, ey, color, width, name="", opacity=1.0) -> dict:
    return {"x": ex, "y": ey, "ax": sx, "ay": sy, "xref": "x", "yref": "y", "axref": "x", "ayref": "y", "showarrow": True, "text": "",
            "arrowhead": 2, "arrowsize": max(0.6, 1.4 - width / 12), "arrowwidth": width, "arrowcolor": color, "opacity": opacity, "name": name}


def _title(text: str, sub: str = "") -> str:
    return f"<b>{text}</b>" + (f"<br><span style='font-size:13px'>{sub}</span>" if sub else "")


def shots(feed: dict) -> list[dict]:
    return [a for a in feed["actions"] if a.get("shot")]


def moments(feed: dict, min_xg: float = BIG_CHANCE_XG) -> list[dict]:
    """Goals (counted or ruled out) and big chances, in match order: what `buildup_figure` can draw."""
    return [a for a in shots(feed) if a["shot"].get("outcome") == "goal" or a["shot"].get("xg", 0) >= min_xg]


def set_pieces(feed: dict) -> list[dict]:
    return [a for a in feed["actions"] if a.get("action_type") in SET_PIECES | DIRECT]


# --- shot map ---------------------------------------------------------------------------------------

XG_KEY = (0.05, 0.2, 0.5)  # reference circles of the size key under the pitch
KEY_DEPTH = 9.0  # metres of room under the pitch for the size key


def shot_size(xg: float) -> float:
    """Marker diameter in pixels for a shot of `xg`: the area grows with the xG."""
    return 8 + 34 * math.sqrt(max(xg, 0.0))


def shot_filter(a: dict, team: str | None = None, half: int | None = None) -> bool:
    """Whether a shot is in the map: of `team` (None: both) and in `half` (1: first half, 2: second half and extra time, None: the match)."""
    return (team is None or a["team"] == team) and (half is None or (a.get("period", 1) >= 2) == (half == 2))


def shot_map(feed: dict, theme="dark", width: int | None = 900, team: str | None = None, half: int | None = None) -> go.Figure:
    """Every shot, or those of `team` or of one `half`, with a size key under the pitch (xG 0.05, 0.2, 0.5)."""
    t = _t(theme)
    teams = team_names(feed.get("info"))
    fig = pitch.pitch_figure(theme, width, y_range=(-pitch.PAD - 1 - KEY_DEPTH, pitch.WIDTH + pitch.PAD + 1))
    picked = [a for a in shots(feed) if shot_filter(a, team, half)]
    by_outcome: dict[str, list[dict]] = {}
    for a in picked:
        by_outcome.setdefault(_outcome(a), []).append(a)
    for key, (label, color, symbol, filled) in OUTCOMES.items():
        items = by_outcome.get(key, [])
        if not items:
            continue
        pts = [_to_home(a, a["start_x"], a["start_y"]) for a in items]
        fig.add_trace(go.Scatter(
            x=[p[0] for p in pts], y=[p[1] for p in pts], mode="markers", name=label, ids=[a["event_id"] for a in items],
            marker={"size": [shot_size(a["shot"]["xg"]) for a in items], "symbol": symbol, "color": color,
                    "line": {"color": t["paper"] if filled else color, "width": 1.5 if filled else 2.2}, "opacity": 0.95},
            customdata=[[a.get("minute_label", ""), teams[a["team"]], _who(a), a["shot"]["xg"], label + (" (penalty)" if a["action_type"] == "shot_penalty" else " (free kick)" if a["action_type"] == "shot_freekick" else "")] for a in items],
            hovertemplate="%{customdata[0]} %{customdata[1]} %{customdata[2]}<br>xG %{customdata[3]:.2f}, %{customdata[4]}<extra></extra>"))
    key_y = -pitch.PAD - 1 - KEY_DEPTH / 2
    key_x = [pitch.HALF_X - 6 + 13 * k for k in range(len(XG_KEY))]
    fig.add_trace(go.Scatter(x=key_x, y=[key_y] * len(XG_KEY), mode="markers+text", name="xG key", text=[f" xG {v:g}" for v in XG_KEY], textposition="middle right",
                             textfont={"color": t["muted"], "size": 12}, hoverinfo="skip", showlegend=False,
                             marker={"size": [shot_size(v) for v in XG_KEY], "symbol": "circle-open", "color": t["muted"], "line": {"width": 1.6}}))
    fig.add_annotation(x=key_x[0] - 3, y=key_y, text="Marker size:", showarrow=False, xanchor="right", font={"color": t["muted"], "size": 12})
    counted = [a for a in picked if not a.get("voided")]  # a shot VAR ruled out stays drawn but is out of the totals, as in the insight facts
    xg = {s: sum(a["shot"]["xg"] for a in counted if a["team"] == s) for s in ("home", "away")}
    goals = {s: sum(1 for a in counted if a["team"] == s and T.counted_goal(a)) for s in ("home", "away")}
    n = {s: sum(1 for a in counted if a["team"] == s) for s in ("home", "away")}
    for side, x, anchor in (("away", 2, "left"), ("home", pitch.LENGTH - 2, "right")):
        if team in (None, side):
            fig.add_annotation(x=x, y=pitch.WIDTH + 1.2, text=f"<b>{teams[side]}</b> {goals[side]} goals, {n[side]} shots, xG {xg[side]:.2f}", showarrow=False,
                               xanchor=anchor, yanchor="bottom", font={"color": t[side], "size": 13})
    scope = teams[team] if team else "First half" if half == 1 else "Second half" if half == 2 else "Both teams"
    fig.update_layout(title_text=_title("Shot map", f"{scope}. Bigger marker, better chance (xG). {teams['home']} attack right, {teams['away']} left"), showlegend=True,
                      legend={"orientation": "h", "y": -0.01, "x": 0, "yanchor": "top", "font": {"color": t["text"]}, "itemsizing": "constant"},
                      meta={"filter": {"team": team, "half": half}, "shots": len(picked)})
    if width:
        fig.update_layout(height=fig.layout.height + 40, margin={"b": 50})
    return fig


# --- build-ups --------------------------------------------------------------------------------------

CARRIES = {"dribble", "take_on"}
CROSSES = {"cross", "corner_crossed", "freekick_crossed"}
WINS = {"tackle": "tackle", "interception": "interception", "clearance": "clearance", "keeper_claim": "claim", "keeper_save": "save"}
RESTART_WINS = {"throw_in": "throw-in", "goalkick": "goal kick", "corner_short": "corner", "corner_crossed": "corner", "freekick_short": "free kick", "freekick_crossed": "free kick"}
# how each kind of action is drawn: legend name, line dash, whether the path is curved
STROKES = {"pass": ("Pass", "solid", False), "carry": ("Carry or dribble", "dash", False), "cross": ("Cross", "dot", True), "shot": ("Shot", "solid", False)}


def _possession(feed: dict, event_id: str, n_max: int = 10) -> list[dict]:
    return T.chain(feed["actions"], event_id)[-n_max:]


def arrow_width(threat: float) -> float:
    """Arrow width in pixels for a threat added (xG units): 1.5 px for nothing, up to 12 px."""
    return round(min(12.0, 1.5 + 60 * max(threat, 0.0)), 2)


def stroke(a: dict) -> str:
    """How an action of a build-up is drawn: "shot", "carry" (dribble, take-on), "cross" (crosses and crossed set pieces), "won" (a tackle, interception or other action that wins the ball) or "pass" (any other ball played)."""
    if a.get("shot"):
        return "shot"
    if a["action_type"] in CARRIES:
        return "carry"
    if a["action_type"] in CROSSES:
        return "cross"
    if a["action_type"] in WINS:
        return "won"
    return "pass"


def ball_won(feed: dict, seq: list[dict]) -> dict:
    """Where and how the team won the ball at the start of the possession that `seq` ends (the whole possession, even when `seq` is cut): `{how, x, y, metres, high, event_id}` in the team's frame, `metres` from its own goal line, `high` when won from the opponent in the opponent's half. `how` is "kept the ball" when the possession follows one of the same team."""
    first = T.chain(feed["actions"], seq[-1]["event_id"])[0]
    acts = feed["actions"]
    i = next(k for k, a in enumerate(acts) if a["event_id"] == first["event_id"])
    prev = acts[i - 1] if i > 0 and acts[i - 1].get("period") == first.get("period") else None
    if first["action_type"] in WINS:
        how = WINS[first["action_type"]]
    elif first["action_type"] in RESTART_WINS:
        how = RESTART_WINS[first["action_type"]]
    elif prev is not None and prev["team"] != first["team"]:
        how = "loose ball" if prev.get("result") != "success" else f"after a {pitch.ACTION_LABELS.get(prev['action_type'], prev['action_type'].replace('_', ' '))}"
    else:
        how = "kept the ball"
    x, y = pitch.clamp(first["start_x"], first["start_y"])
    return {"how": how, "x": x, "y": y, "metres": round(x), "high": x > pitch.HALF_X and how != "kept the ball", "event_id": first["event_id"]}


def _lower(label: str) -> str:
    return label[:1].lower() + label[1:]


def _receiver(seq: list[dict], k: int) -> dict | None:
    nxt = seq[k + 1] if k + 1 < len(seq) else None
    return nxt if nxt is not None and nxt["team"] == seq[k]["team"] else None


def buildup_steps(feed: dict, seq: list[dict]) -> list[str]:
    """The build-up in words, one line per action: "2. #3 Name passes to #11 Name", "6. #20 Name shoots, xG 0.31, goal"."""
    won = ball_won(feed, seq)
    out = []
    for k, a in enumerate(seq):
        who, kind = _who(a), stroke(a)
        rec = _receiver(seq, k)
        to = f" to {_who(rec)}" if rec is not None and rec["player"]["id"] != a["player"]["id"] else ""
        if kind == "shot":
            what = f"shoots, xG {a['shot']['xg']:.2f}, {_lower(OUTCOMES[_outcome(a)][0])}"
        elif kind == "won":
            what = f"wins it ({won['how']})" if won["event_id"] == a["event_id"] else WINS[a["action_type"]]
        elif kind == "carry":
            what = f"carries it {math.hypot(a['end_x'] - a['start_x'], a['end_y'] - a['start_y']):.0f} m"
        elif kind == "cross":
            what = f"crosses{to}"
        else:
            what = f"passes{to}" + (", flagged offside" if a.get("result") == "offside" else ", lost" if a.get("result") == "fail" else "")
        if k == 0 and kind != "won" and won["how"] != "kept the ball" and won["event_id"] == a["event_id"]:
            what += f" (ball won: {won['how']})"
        out.append(f"{k + 1}. {who} {what}")
    return out


def _wrap(parts: list[str], width: int = 120) -> list[str]:
    lines, cur = [], ""
    for p in parts:
        if cur and len(cur) + 3 + len(p) > width:
            lines.append(cur)
            cur = p
        else:
            cur = f"{cur}   {p}" if cur else p
    return lines + ([cur] if cur else [])


def _curve(sx, sy, ex, ey, bend: float = 0.18, n: int = 12) -> tuple[list[float], list[float]]:
    """A quadratic curve from start to end bowed to the left of the direction of travel by `bend` of its length."""
    mx, my = (sx + ex) / 2, (sy + ey) / 2
    cx, cy = mx - (ey - sy) * bend, my + (ex - sx) * bend
    ts = [i / n for i in range(n + 1)]
    return [(1 - u) ** 2 * sx + 2 * (1 - u) * u * cx + u * u * ex for u in ts], [(1 - u) ** 2 * sy + 2 * (1 - u) * u * cy + u * u * ey for u in ts]


def buildup_figure(feed: dict, event_id: str, theme="dark", width: int | None = 900, n_max: int = 10) -> go.Figure:
    """The possession that ended with `event_id`, numbered in order: passes as solid arrows, carries dashed, crosses dotted and curved, the shot thick and coloured by its outcome (widths by threat added, the shot's by its xG); receivers as dots, where and how the ball was won starred (gold when in the opponent's half), and the sequence in words under the pitch. The attacking team attacks right, over the players at the final action."""
    t = _t(theme)
    teams = team_names(feed.get("info"))
    names = player_names(feed.get("info"))
    seq = _possession(feed, event_id, n_max)
    last = seq[-1]
    fig = pitch.pitch_figure(theme, width)
    traces = pitch.frame_traces(last, theme, last["team"], names)
    for tr in traces[:2]:
        tr.marker.opacity = 0.45
        tr.showlegend = False
    fig.add_traces(traces[:2])
    color = t[last["team"]]
    starts, shown = [], set()
    for k, a in enumerate(seq):
        view = (lambda x, y: (x, y)) if a["team"] == last["team"] else (lambda x, y: (pitch.LENGTH - x, pitch.WIDTH - y))
        sx, sy = pitch.clamp(*view(a["start_x"], a["start_y"]))
        ex, ey = pitch.clamp(*view(a["end_x"], a["end_y"]))
        starts.append((sx, sy))
        kind = stroke(a)
        if kind == "won" or math.hypot(ex - sx, ey - sy) < 0.5:
            continue
        if kind == "shot":
            w, c = max(5.0, arrow_width(a["shot"]["xg"])), OUTCOMES[_outcome(a)][1]
        else:
            w, c = max(2.0, arrow_width(T.threat_added(a))), color
        if a["team"] != last["team"]:
            c = t["muted"]
        name, dash, curved = STROKES[kind]
        length = math.hypot(ex - sx, ey - sy)
        cut = 0.0 if kind == "shot" else min(2.2, 0.3 * length) / length  # the arrowhead stops short of the next numbered dot
        tx, ty = ex - (ex - sx) * cut, ey - (ey - sy) * cut
        xs, ys = _curve(sx, sy, tx, ty) if curved else ([sx, tx], [sy, ty])
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines+markers", name=name, legendgroup=kind, showlegend=kind not in shown, hoverinfo="skip",
                                 line={"color": c, "width": w, "dash": dash}, marker={"symbol": ["circle"] * (len(xs) - 1) + ["arrow"], "size": [0] * (len(xs) - 1) + [10 + w],
                                                                                    "angleref": "previous", "color": c}, meta={"step": k + 1, "kind": kind}))
        shown.add(kind)
    steps = buildup_steps(feed, seq)
    won = ball_won(feed, seq)
    before = len(T.chain(feed["actions"], event_id)) - len(seq)  # actions of the possession before the first one drawn
    wx, wy = won["x"], won["y"]  # in the attacking team's frame, as drawn
    gold = t["actor"]
    nx, ny = next(((x, y) for x, y in starts if math.hypot(x - wx, y - wy) >= 1.5), (pitch.HALF_X, pitch.MID_Y))
    away = math.hypot(nx - wx, ny - wy) or 1.0
    ax, ay = -40 * (nx - wx) / away, 30 * (ny - wy) / away  # the label on the side away from the next action (screen y points down)
    ax, ay = ax if abs(ax) > 8 else 0, ay if abs(ay) > 8 else (-30 if wy < pitch.MID_Y else 30)
    fig.add_trace(go.Scatter(x=[wx], y=[wy], mode="markers", name=("Ball won high" if won["high"] else "Ball won") if won["how"] != "kept the ball" else "Possession start", hoverinfo="skip",
                             marker={"symbol": "star", "size": 30 if won["high"] else 24, "color": gold if won["high"] else "rgba(0,0,0,0)", "opacity": 0.85,
                                     "line": {"color": gold, "width": 2}}))
    fig.add_annotation(x=wx, y=wy, text=((f"<b>Won here</b>: {won['how']}, {won['metres']} m from own goal" + (" (high regain)" if won["high"] else "")) if won["how"] != "kept the ball"
                       else f"<b>Possession starts here</b>, {won['metres']} m from own goal") + (f", {before} actions before 1" if before else ""), showarrow=True, arrowhead=0,
                       arrowcolor=gold, ax=ax, ay=ay, font={"color": gold, "size": 12}, bgcolor=t["paper"], bordercolor=gold, borderpad=3, name="won")
    dots: list[list] = []  # [x, y, labels, hover lines]: one dot per place the ball was played from, so a receiver who plays at once shares his dot
    for k, (x, y) in enumerate(starts):
        if dots and math.hypot(x - dots[-1][0], y - dots[-1][1]) < 1.5:
            dots[-1][2].append(str(k + 1))
            dots[-1][3].append(steps[k])
        else:
            dots.append([x, y, [str(k + 1)], [steps[k]]])
    fig.add_trace(go.Scatter(x=[d[0] for d in dots], y=[d[1] for d in dots], mode="markers+text", name="On the ball, in order", text=[",".join(d[2]) for d in dots],
                             textfont={"color": "#111827", "size": 12, "family": t["font"]}, hovertext=["<br>".join(d[3]) for d in dots], hoverinfo="text",
                             marker={"size": [19 + 7 * (len(d[2]) - 1) for d in dots], "color": "#ffffff", "line": {"color": "#111827", "width": 1.5}}))
    lines = _wrap(steps)
    fig.add_annotation(x=0, y=0, xref="paper", yref="paper", xanchor="left", yanchor="top", yshift=-30, align="left", showarrow=False, text="<br>".join(lines),
                       font={"color": t["text"], "size": 12}, name="sequence")
    s = last.get("score", {"home": 0, "away": 0})
    head = f"{last.get('minute_label', '')}  {teams['home']} {s['home']}-{s['away']} {teams['away']}"
    sub = f"{teams[last['team']]} build-up to {_who(last)}'s " + ("goal" if T.any_goal(last) else "chance") + (" (ruled out by VAR)" if last.get("voided") else "") \
        + ". Numbered in order; width: threat added"
    fig.update_layout(title_text=_title(head, sub), showlegend=True, margin={"b": 40 + 17 * len(lines)},
                      legend={"orientation": "h", "y": 0, "x": 0, "yanchor": "top", "font": {"color": t["text"], "size": 12}, "itemsizing": "constant"},
                      meta={"buildup": [a["event_id"] for a in seq], "sequence": steps, "won": won})
    if width:
        fig.update_layout(height=fig.layout.height + 30 + 17 * len(lines))
    return fig


# --- set pieces -------------------------------------------------------------------------------------

def first_contact(feed: dict, event_id: str) -> dict | None:
    acts = feed["actions"]
    i = next(k for k, a in enumerate(acts) if a["event_id"] == event_id)
    return acts[i + 1] if i + 1 < len(acts) and acts[i + 1].get("period") == acts[i].get("period") else None


def _in_view(a: dict, view: str, x: float, y: float) -> tuple[float, float]:
    return pitch.clamp(*((x, y) if a["team"] == view else (pitch.LENGTH - x, pitch.WIDTH - y)))


def set_piece_figure(feed: dict, event_id: str, theme="dark", width: int | None = 900, duration_ms: int = 1300) -> go.Figure:
    """A corner or free kick in up to three animated steps: the delivery, the first contact, what the contact did. Arrows: delivery width by the value of where it landed, contact width by threat added (or xG for a shot)."""
    t = _t(theme)
    teams = team_names(feed.get("info"))
    names = player_names(feed.get("info"))
    acts = feed["actions"]
    sp = next(a for a in acts if a["event_id"] == event_id)
    view = sp["team"]
    i = acts.index(sp)
    steps = [sp] + [a for a in acts[i + 1:i + 3] if a.get("period") == sp.get("period")]
    arrows, frames = [], []
    for k, a in enumerate(steps):
        sx, sy = _in_view(a, view, a["start_x"], a["start_y"])
        ex, ey = _in_view(a, view, a["end_x"], a["end_y"])
        if k == 0:
            w = arrow_width(T.location_value(a["end_x"], a["end_y"]))
            c = t[view]
            label = f"Delivery by {_who(a)}"
        elif a.get("shot"):
            w, c = arrow_width(a["shot"]["xg"]), OUTCOMES[_outcome(a)][1]
            label = f"{'First contact' if k == 1 else 'Then'}: {_who(a)} shoots, xG {a['shot']['xg']:.2f}, {OUTCOMES[_outcome(a)][0].lower()}"
        else:
            won = a["team"] == view
            w, c = (arrow_width(T.threat_added(a)) if won else 2.0), (t[view] if won else t["muted"])
            label = f"{'First contact' if k == 1 else 'Then'}: {teams[a['team']]} {_who(a)}, {a['action_type'].replace('_', ' ')}"
        if math.hypot(ex - sx, ey - sy) >= 0.5:
            arrows.append(_arrow(sx, sy, ex, ey, c, w, name=f"step-{k}"))
        s = a.get("score", {"home": 0, "away": 0})
        head = f"{sp.get('minute_label', '')}  {teams['home']} {s['home']}-{s['away']} {teams['away']}: {teams[view]} {pitch.ACTION_LABELS.get(sp['action_type'], sp['action_type'])}"
        frames.append(go.Frame(name=str(k), data=pitch.frame_traces(a, theme, view, names), traces=list(range(5)),
                               layout={"annotations": list(arrows), "title": {"text": _title(head, f"{k + 1}/{len(steps)}. {label}")}}))
        if a.get("shot") or (k >= 1 and a["team"] != view and a["action_type"] in ("clearance", "keeper_claim", "keeper_save", "interception")):
            break
    attacking_end = sp["start_x"] > pitch.HALF_X or sp["action_type"].startswith("corner")
    xr = (pitch.HALF_X - 2, pitch.LENGTH + pitch.PAD + 1) if attacking_end else None
    fig = pitch.pitch_figure(theme, width, x_range=xr)
    fig.add_traces(frames[0].data)
    fig.frames = frames
    play = {"frame": {"duration": duration_ms, "redraw": True}, "transition": {"duration": int(duration_ms * 0.5)}, "fromcurrent": True}
    still = {"frame": {"duration": 0, "redraw": True}, "transition": {"duration": 0}, "mode": "immediate"}
    fig.update_layout(
        title_text=frames[0].layout.title.text, annotations=frames[0].layout.annotations, margin={"b": 70},
        updatemenus=[{"type": "buttons", "direction": "left", "x": 0.02, "y": -0.02, "xanchor": "left", "yanchor": "top", "bgcolor": t["paper"], "bordercolor": t["muted"],
                      "font": {"color": t["text"]}, "buttons": [{"label": "Play", "method": "animate", "args": [None, play]}, {"label": "Pause", "method": "animate", "args": [[None], still]}]}],
        sliders=[{"x": 0.25, "y": -0.02, "len": 0.7, "xanchor": "left", "yanchor": "top", "currentvalue": {"visible": False}, "font": {"color": t["muted"]},
                  "steps": [{"label": str(k + 1), "method": "animate", "args": [[str(k)], still]} for k in range(len(frames))]}],
        meta={"steps": [a["event_id"] for a in steps[:len(frames)]]})
    if width:
        fig.update_layout(height=fig.layout.height + 60)
    return fig


def set_pieces_map(feed: dict, team: str, theme="dark", width: int | None = 900) -> go.Figure:
    """Every corner and crossed free kick of `team`, drawn attacking right: arrow width by the value of the landing point, team colour when the first contact was won, grey when lost."""
    t = _t(theme)
    teams = team_names(feed.get("info"))
    fig = pitch.pitch_figure(theme, width, x_range=(pitch.HALF_X - 2, pitch.LENGTH + pitch.PAD + 1))
    won = lost = 0
    hover = []
    for a in feed["actions"]:
        if a["team"] != team or a["action_type"] not in ("corner_crossed", "freekick_crossed", "corner_short", "freekick_short"):
            continue
        nxt = first_contact(feed, a["event_id"])
        ok = nxt is not None and nxt["team"] == team
        won += ok
        lost += not ok
        sx, sy = pitch.clamp(a["start_x"], a["start_y"])
        ex, ey = pitch.clamp(a["end_x"], a["end_y"])
        fig.add_annotation(**_arrow(sx, sy, ex, ey, t[team] if ok else t["muted"], arrow_width(T.location_value(a["end_x"], a["end_y"])), opacity=0.9))
        hover.append((ex, ey, f"{a.get('minute_label', '')} {_who(a)}: first contact {'won' if ok else 'lost'}" + (f" by {_who(nxt)}" if nxt else "")))
    fig.add_trace(go.Scatter(x=[h[0] for h in hover], y=[h[1] for h in hover], mode="markers", marker={"size": 9, "color": t["ball"], "line": {"color": t["ball_edge"], "width": 1}},
                             hovertext=[h[2] for h in hover], hoverinfo="text", name="landing"))
    fig.update_layout(title_text=_title(f"{teams[team]} set pieces", f"{won + lost} deliveries, first contact won {won}, lost {lost}. Arrow width: value of the landing point"))
    return fig


# --- momentum ---------------------------------------------------------------------------------------

RED_CARDS = {"red", "second_yellow"}
MOMENTUM_SLICES = (5, 3, 1)  # minutes per bar offered in the picker


def _first_half_extra(feed: dict) -> float:
    top = max((a["clock_s"] for a in feed["actions"] if a.get("period") == 1), default=2700.0)
    return max(0.0, (top - 2700.0) / 60)


def _x(period: int, clock_s: float, extra: float) -> float:
    return clock_s / 60 + (extra if period and period >= 2 else 0.0)


def _minute_axis(extra: float) -> dict:
    return {"tickvals": [0, 15, 30, 45] + [m + extra for m in (60, 75, 90)], "ticktext": ["0'", "15'", "30'", "45'", "60'", "75'", "90'"], "gridcolor": "rgba(148,163,184,0.15)"}


def momentum_series(feed: dict, minutes: int = 5) -> dict:
    """Threat added and xG per slice of `minutes` per team: `{home: [{x, period, index, threat, xg}], away: [...]}`, x the slice's middle on a continuous minute axis. Shots VAR ruled out are not in the xG."""
    extra = _first_half_extra(feed)
    out = {}
    for side in ("home", "away"):
        mine = [a for a in feed["actions"] if a["team"] == side]
        threat = {(b["period"], b["index"]): b for b in T.per_bucket(mine, lambda a: max(T.threat_added(a), 0.0), minutes, sum)}
        xg = {(b["period"], b["index"]): b for b in T.per_bucket([a for a in mine if a.get("shot") and not a.get("voided")], lambda a: a["shot"]["xg"], minutes, sum)}
        keys = sorted(set(threat) | set(xg))
        out[side] = [{"x": _x(p, T.PERIOD_START_S.get(p, 0) + k * minutes * 60, extra) + minutes / 2, "period": p, "index": k, "threat": round(threat.get((p, k), {}).get("value", 0.0), 4),
                      "xg": round(xg.get((p, k), {}).get("value", 0.0), 3)} for p, k in keys]
    return out


def rolling_threat(series: list[dict], minutes: int, extra: float, window: int) -> list[tuple[float, float]]:
    """(x, mean threat of the `window` slices centred on it) for every slice of every period, empty slices counted as 0, the mean taken within a period."""
    out = []
    for period in sorted({p["period"] for p in series}):
        vals = {p["index"]: p["threat"] for p in series if p["period"] == period}
        dense = [vals.get(k, 0.0) for k in range(max(vals) + 1)]
        half = window // 2
        for k in range(len(dense)):
            part = dense[max(0, k - half):k + half + 1]
            out.append((_x(period, T.PERIOD_START_S.get(period, 0) + k * minutes * 60, extra) + minutes / 2, round(sum(part) / len(part), 4)))
    return out


def match_marks(feed: dict) -> list[dict]:
    """Goals (counted, labelled with the score after them, or ruled out by VAR) and red cards, in match order: `{kind, label, team, period, clock_s, minute_label, event_id}`."""
    score = {"home": 0, "away": 0}
    out = []
    for a in feed["actions"]:
        if T.any_goal(a):
            if a.get("voided"):
                out.append({"kind": "voided", "label": f"× {a.get('minute_label', '')} VAR"})
            else:
                score[a["team"]] += 1
                out.append({"kind": "goal", "label": f"{score['home']}-{score['away']} {a.get('minute_label', '')}"})
        elif a.get("card") in RED_CARDS:
            out.append({"kind": "red", "label": f"{a.get('minute_label', '')} red card"})
        else:
            continue
        out[-1].update(team=a["team"], period=a.get("period", 1), clock_s=a["clock_s"], minute_label=a.get("minute_label", ""), event_id=a["event_id"])
    return out


def label_rows(xs: list[float], gap: float) -> list[int]:
    """A row per label, so that two labels on one row are at least `gap` apart (xs in increasing order)."""
    last: list[float] = []
    out = []
    for x in xs:
        k = next((i for i, v in enumerate(last) if x - v >= gap), len(last))
        if k == len(last):
            last.append(x)
        else:
            last[k] = x
        out.append(k)
    return out


def _vmarks(fig: go.Figure, marks: list[dict], t: dict, extra: float, ceiling: float, step: float, yref: str = "y") -> int:
    """Goal and red-card lines from -`ceiling` to `ceiling`, home labels above, away labels below, one row every `step`; returns the number of rows used."""
    rows = 0
    for side, sign in (("home", 1), ("away", -1)):
        mine = [m for m in marks if m["team"] == side]
        xs = [_x(m["period"], m["clock_s"], extra) for m in mine]
        for m, x, row in zip(mine, xs, label_rows(xs, 7.5)):
            rows = max(rows, row + 1)
            color = t["text"] if m["kind"] == "red" else t[side]
            fig.add_shape(type="line", x0=x, x1=x, y0=-ceiling, y1=ceiling, yref=yref, name=m["kind"],
                          line={"color": color, "width": 2 if m["kind"] == "goal" else 1.5, "dash": {"goal": "solid", "voided": "dash", "red": "dot"}[m["kind"]]})
            fig.add_annotation(x=x, y=sign * (ceiling + step * row), yref=yref, text=f"<b>{m['label']}</b>" if m["kind"] == "goal" else m["label"], showarrow=False,
                               yanchor="bottom" if sign > 0 else "top", font={"size": 11, "color": color}, bgcolor=t["paper"], name=m["kind"])
    return rows


def momentum_figure(feed: dict, timeline: list[dict] | None = None, theme="dark", minutes: int = 5, width: int | None = 900) -> go.Figure:
    """Mirrored bars of threat added per `minutes` (home up, away down) under a rolling average line per team; goals as vertical lines in the scorer's colour with the score (dashed with a cross when VAR ruled them out), red cards dotted, half-time marked; the desk's win probability on a right-hand axis when a `timeline` is given (the home team's, the draw and the away win in its hover)."""
    t = _t(theme)
    teams = team_names(feed.get("info"))
    extra = _first_half_extra(feed)
    series = momentum_series(feed, minutes)
    window = max(3, round(15 / minutes))
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    top = max([abs(p["threat"]) for s in series.values() for p in s] + [0.1])
    for side, sign in (("home", 1), ("away", -1)):
        pts = series[side]
        fig.add_trace(go.Bar(x=[p["x"] for p in pts], y=[sign * p["threat"] for p in pts], name=f"{teams[side]} threat", marker={"color": t[side], "line": {"width": 0}, "opacity": 0.5},
                             width=minutes * 0.85, customdata=[[p["threat"], p["xg"]] for p in pts], legendgroup=side,
                             hovertemplate=f"{teams[side]}<br>threat added %{{customdata[0]:.2f}}<br>shots xG %{{customdata[1]:.2f}}<extra></extra>"), secondary_y=False)
    for side, sign in (("home", 1), ("away", -1)):
        roll = rolling_threat(series[side], minutes, extra, window)
        fig.add_trace(go.Scatter(x=[x for x, _ in roll], y=[sign * v for _, v in roll], mode="lines", name=f"{teams[side]} {window * minutes}-min average", legendgroup=side,
                                 line={"color": t[side], "width": 3, "shape": "spline", "smoothing": 0.6, "dash": "solid" if side == "home" else "dash"}, hoverinfo="skip"), secondary_y=False)
    if timeline:
        period_of = {a["event_id"]: a.get("period", 1) for a in feed["actions"]}
        period, pts = 1, []
        for p in sorted(timeline, key=lambda p: p["seq"]):
            period = period_of.get(p.get("event_id"), period)
            pts.append((_x(period, p["clock_s"], extra), p))
        fig.add_trace(go.Scatter(x=[x for x, _ in pts], y=[p["home"] * 100 for _, p in pts], mode="lines", name=f"{teams['home']} win probability (right axis)",
                                 line={"color": t["text"], "width": 1.3, "shape": "hv"}, opacity=0.7, customdata=[[p.get("minute_label", ""), p["draw"] * 100, p["away"] * 100] for _, p in pts],
                                 hovertemplate=f"%{{customdata[0]}} {teams['home']} win %{{y:.0f}}%, draw %{{customdata[1]:.0f}}%, {teams['away']} win %{{customdata[2]:.0f}}%<extra></extra>"),
                      secondary_y=True)
        fig.update_yaxes(range=[-5, 105], tickvals=[0, 25, 50, 75, 100], ticktext=["0%", "25%", "50%", "75%", "100%"], showgrid=False, zeroline=False, title_text="win probability", secondary_y=True)
    ceiling, step = top * 1.2, top * 0.17
    rows = _vmarks(fig, match_marks(feed), t, extra, ceiling, step)
    span = ceiling + step * max(rows, 1) + top * 0.05
    fig.add_vline(x=45 + extra, line={"color": t["muted"], "width": 1, "dash": "dot"})
    fig.add_annotation(x=45 + extra, y=-span, yref="y", text="HT", showarrow=False, yanchor="bottom", xanchor="left", xshift=3, font={"size": 11, "color": t["muted"]})
    fig.update_yaxes(range=[-span, span], zeroline=True, zerolinecolor=t["muted"], gridcolor="rgba(148,163,184,0.15)", title_text="threat added (xG)", secondary_y=False)
    fig.update_xaxes(**_minute_axis(extra))
    wp = f"; thin line, right axis: {teams['home']} win probability" if timeline else ""
    sub = (f"Bars: threat added per {minutes} min, {teams['home']} up, {teams['away']} down; thick lines: {window * minutes}-min average{wp}"
           "<br>Solid verticals: goals and the score; dashed with ×: ruled out by VAR; dotted: red cards")
    fig.update_layout(paper_bgcolor=t["paper"], plot_bgcolor=t["paper"], font={"family": t["font"], "color": t["text"], "size": 12}, barmode="relative", bargap=0.1,
                      title={"text": _title("Momentum", sub), "x": 0.02, "xanchor": "left"},
                      legend={"orientation": "h", "y": -0.1, "x": 0, "yanchor": "top"}, margin={"l": 60, "r": 60 if timeline else 16, "t": 96, "b": 60}, height=480, hovermode="closest",
                      meta={"minutes": minutes, "window": window, "marks": [m["event_id"] for m in match_marks(feed)]})
    if width:
        fig.update_layout(width=width)
    return fig


# --- penalties and direct free kicks ---------------------------------------------------------------------

def shot_on_goal_figure(feed: dict, event_id: str, theme="dark", width: int | None = 700) -> go.Figure:
    """The goal seen from behind the taker (his right on the right): where the ball crossed the line (end_y, end_z) and where the keeper stood in the frame."""
    t = _t(theme)
    teams = team_names(feed.get("info"))
    a = next(x for x in feed["actions"] if x["event_id"] == event_id)
    # facing the goal at x = 105 with y = 0 on the right: horizontal = WIDTH - y, centred on the goal
    to_h = lambda y: (pitch.WIDTH - y) - pitch.MID_Y  # noqa: E731
    fig = go.Figure()
    half = GOAL_W / 2
    fig.add_shape(type="rect", x0=-12, x1=12, y0=-0.6, y1=0, fillcolor=t["grass"], line={"width": 0}, layer="below")
    fig.add_shape(type="path", path=f"M {-half},0 L {-half},{GOAL_H} L {half},{GOAL_H} L {half},0", line={"color": t["text"], "width": 5})
    for k in range(1, 8):
        x = -half + k * GOAL_W / 8
        fig.add_shape(type="line", x0=x, x1=x, y0=0, y1=GOAL_H, line={"color": "rgba(230,235,240,0.15)", "width": 1}, layer="below")
    keeper = next((p for p in a.get("freeze_frame") or [] if not p["teammate"] and p.get("keeper")), None)
    if keeper:
        kx = to_h(keeper["location"][1])
        fig.add_shape(type="rect", x0=kx - 0.25, x1=kx + 0.25, y0=0, y1=1.9, fillcolor=t["away" if a["team"] == "home" else "home"], opacity=0.6, line={"width": 0})
        fig.add_annotation(x=kx, y=1.95, text="keeper", showarrow=False, yanchor="bottom", font={"color": t["muted"], "size": 11})
    shot = a.get("shot") or {}
    o = _outcome(a)
    bx, bz = to_h(a["end_y"]), shot.get("end_z", 0.3)
    fig.add_trace(go.Scatter(x=[bx], y=[bz], mode="markers", marker={"size": 22, "color": OUTCOMES[o][1], "symbol": "circle", "line": {"color": t["paper"], "width": 2}},
                             hovertext=[f"{OUTCOMES[o][0]}, xG {shot.get('xg', 0):.2f}"], hoverinfo="text", name="ball"))
    kind = "Penalty" if a["action_type"] == "shot_penalty" else "Free kick"
    s = a.get("score", {"home": 0, "away": 0})
    fig.update_layout(
        title_text=_title(f"{a.get('minute_label', '')}  {teams['home']} {s['home']}-{s['away']} {teams['away']}",
                          f"{kind}: {teams[a['team']]} {_who(a)}, xG {shot.get('xg', 0):.2f}, {OUTCOMES[o][0].lower()}. Seen from behind the taker"),
        xaxis={"range": [-8, 8], "visible": False, "fixedrange": True}, yaxis={"range": [-0.6, 4], "visible": False, "fixedrange": True, "scaleanchor": "x", "scaleratio": 1},
        paper_bgcolor=t["paper"], plot_bgcolor=t["paper"], font={"family": t["font"], "color": t["text"]}, showlegend=False, margin={"l": 10, "r": 10, "t": 70, "b": 10},
        meta={"ball_h": round(bx, 2), "ball_z": bz})
    if width:
        fig.update_layout(width=width, height=round((width - 20) * 4.6 / 16) + 80)
    return fig


# --- tactical details --------------------------------------------------------------------------------

def _action(feed: dict, event_id: str) -> dict:
    return next(a for a in feed["actions"] if a["event_id"] == event_id)


def line_break_figure(feed: dict, event_id: str, theme="dark", width: int | None = 900) -> go.Figure:
    """The frame of a pass with the band it crossed shaded and every bypassed defender ringed."""
    t = _t(theme)
    a = _action(feed, event_id)
    names = player_names(feed.get("info"))
    teams = team_names(feed.get("info"))
    ids = T.defenders_bypassed(a)
    fig = pitch.pitch_figure(theme, width)
    fig.add_shape(type="rect", x0=a["start_x"], x1=min(a["end_x"], pitch.LENGTH), y0=0, y1=pitch.WIDTH, fillcolor=t["offside"], opacity=0.10, line={"width": 0}, layer="below")
    fig.add_traces(pitch.frame_traces(a, theme, None, names))
    ringed = [p for p in a["freeze_frame"] if p["player_id"] in ids]
    fig.add_trace(go.Scatter(x=[p["location"][0] for p in ringed], y=[p["location"][1] for p in ringed], mode="markers", name="bypassed", hoverinfo="skip",
                             marker={"size": 28, "color": "rgba(0,0,0,0)", "line": {"color": t["text"], "width": 2, "dash": "dot"}}))
    rec = first_contact(feed, event_id)
    sub = f"{teams[a['team']]} {_who(a)} to {_who(rec) if rec else '?'}: {len(ids)} defenders bypassed (ringed), {math.hypot(a['end_x'] - a['start_x'], a['end_y'] - a['start_y']):.1f} m"
    fig.update_layout(title_text=_title(f"{a.get('minute_label', '')}  Line-breaking pass", sub), meta={"bypassed_ids": ids})
    return fig


def run_figure(feed: dict, event_id: str, runner_id: str | None = None, theme="dark", zoom: float | None = None, width: int | None = 900, show_margin: bool = True) -> go.Figure:
    """`pitch.offside_figure` of the pass with the runner; `show_margin=False` leaves the margin out (anyone but the producer)."""
    teams = team_names(feed.get("info"))
    return pitch.offside_figure(_action(feed, event_id), True, teams["home"], teams["away"], theme, runner_id=runner_id, zoom=zoom, names=player_names(feed.get("info")), width=width,
                                show_margin=show_margin)


def space_figure(feed: dict, event_id: str, theme="dark", width: int | None = 900) -> go.Figure:
    """The frame at a reception: a circle around the receiver whose radius is the distance to the nearest opponent, and a line to him."""
    t = _t(theme)
    b = _action(feed, event_id)
    teams = team_names(feed.get("info"))
    rx, ry = T.actor_location(b)
    opp = T.opponents(b, outfield=False)
    near = min(opp, key=lambda p: math.hypot(p["location"][0] - rx, p["location"][1] - ry))
    r = math.hypot(near["location"][0] - rx, near["location"][1] - ry)
    fig = pitch.pitch_figure(theme, width)
    fig.add_shape(type="circle", x0=rx - r, x1=rx + r, y0=ry - r, y1=ry + r, fillcolor=t["actor"], opacity=0.15, line={"color": t["actor"], "width": 1.5})
    fig.add_traces(pitch.frame_traces(b, theme, None, player_names(feed.get("info"))))
    fig.add_shape(type="line", x0=rx, y0=ry, x1=near["location"][0], y1=near["location"][1], line={"color": t["actor"], "width": 2, "dash": "dash"})
    fig.add_annotation(x=(rx + near["location"][0]) / 2, y=(ry + near["location"][1]) / 2, text=f"<b>{r:.1f} m</b>", showarrow=False, yshift=12, font={"color": t["actor"], "size": 15})
    fig.update_layout(title_text=_title(f"{b.get('minute_label', '')}  Space at reception", f"{teams[b['team']]} {_who(b)}: nearest opponent #{pitch.shirt(near['player_id'])} {r:.1f} m away"),
                      meta={"space_m": round(r, 1), "nearest_id": near["player_id"]})
    return fig


def _team_lines(feed: dict, rows: list[dict], value, title: str, sub: str, ytitle: str, theme, minutes: int, width: int | None) -> go.Figure:
    t = _t(theme)
    teams = team_names(feed.get("info"))
    extra = _first_half_extra(feed)
    fig = go.Figure()
    for side in ("home", "away"):
        pts = T.per_bucket(rows, lambda r, s=side: value(r, s), minutes)
        xs = [_x(p["period"], p["start_min"] * 60, extra) + minutes / 2 for p in pts]
        fig.add_trace(go.Scatter(x=xs, y=[round(p["value"], 2) for p in pts], mode="lines+markers", name=teams[side], line={"color": t[side], "width": 2},
                                 marker={"size": 8, "color": t[side], "line": {"color": t["paper"], "width": 2}}, customdata=[p["n"] for p in pts],
                                 hovertemplate=f"{teams[side]} %{{y:.1f}} (%{{customdata}} frames)<extra></extra>"))
    fig.add_vline(x=45 + extra, line={"color": t["muted"], "width": 1, "dash": "dot"})
    fig.update_layout(title={"text": _title(title, sub), "x": 0.02, "xanchor": "left"}, paper_bgcolor=t["paper"], plot_bgcolor=t["paper"],
                      font={"family": t["font"], "color": t["text"], "size": 12}, legend={"orientation": "h", "y": -0.15, "x": 0}, height=380, margin={"l": 50, "r": 16, "t": 80, "b": 50},
                      xaxis={"tickvals": [0, 15, 30, 45] + [m + extra for m in (60, 75, 90)], "ticktext": ["0'", "15'", "30'", "45'", "60'", "75'", "90'"], "gridcolor": "rgba(148,163,184,0.15)"},
                      yaxis={"title": ytitle, "gridcolor": "rgba(148,163,184,0.15)", "rangemode": "tozero"})
    if width:
        fig.update_layout(width=width)
    return fig


def line_height_figure(feed: dict, theme="dark", minutes: int = 5, width: int | None = 900) -> go.Figure:
    rows = T.line_heights(feed["actions"])
    return _team_lines(feed, rows, lambda r, s: r[s] if r["in_possession"] != s else None, "Defensive line height",
                       f"mean distance from a team's own goal line to its deepest outfield player when the opponent has the ball, per {minutes} minutes", "metres", theme, minutes, width)


def pressing_figure(feed: dict, theme="dark", minutes: int = 5, width: int | None = 900) -> go.Figure:
    """Per team, the mean number of its players within `PRESS_RADIUS_M` (5 m) of the opponent on the ball in open play (`worker.tactics.pressure`), titled "Opponents near the ball" for what it counts."""
    rows = T.pressure(feed["actions"])
    return _team_lines(feed, rows, lambda r, s: r["pressers"] if r["pressing_team"] == s else None, "Opponents near the ball",
                       f"mean number of a team's players within {T.PRESS_RADIUS_M:g} m of the opponent on the ball, open play, per {minutes} minutes", "players", theme, minutes, width)


def insight_figure(insight: dict, feed: dict, theme="dark", width: int | None = 900, show_margin: bool = True) -> go.Figure | None:
    """The figure an insight document names in `figure` (`kind` line_break, run, space, line_height, pressing, buildup); None when it names none or the feed lacks the action. `show_margin=False` draws a run without its offside margin."""
    ref = insight.get("figure") or {}
    kind = ref.get("kind")
    try:
        if kind == "line_break":
            return line_break_figure(feed, ref["event_id"], theme, width)
        if kind == "run":
            return run_figure(feed, ref["event_id"], ref.get("runner_id"), theme, width=width, show_margin=show_margin)
        if kind == "space":
            return space_figure(feed, ref["event_id"], theme, width)
        if kind == "line_height":
            return line_height_figure(feed, theme, width=width)
        if kind == "pressing":
            return pressing_figure(feed, theme, width=width)
        if kind == "buildup" or (kind is None and insight.get("event_id")):
            return buildup_figure(feed, ref.get("event_id") or insight["event_id"], theme, width)
    except (StopIteration, KeyError, ValueError):
        return None
    return None
