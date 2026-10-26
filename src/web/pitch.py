"""Pitch visuals for the console and the video, drawn with plotly (shipped with Dash).

Every action of the feed carries a 360-style freeze frame: the players on the pitch with `player_id` ("home-20"), `teammate`, `actor`, `keeper` and `location` [x, y] in metres on a 105 x 68 m pitch seen from the acting team, which attacks towards x = 105 with y = 0 on its right. The figures draw that frame as it is (the default view), or flip it so that a chosen team attacks towards x = 105.

- `pitch_figure`: an empty pitch, dark or light, with a fixed 1:1 aspect ratio.
- `frame_figure`: the players of one action, the ball at the start of the action and an arrow to its end.
- `offside_figure`: the frame of a pass with the offside line at the second-last defender (goalkeeper included) and the runner's margin (left out with `show_margin=False`: the margin is for the producer, never on air or for a visitor).
- `sequence_frames` and `sequence_figure`: the build-up of a moment as plotly animation frames.

This module only depends on plotly, so it runs in the web container, which does not ship the generator.
"""

import math
import re

import plotly.graph_objects as go

LENGTH, WIDTH = 105.0, 68.0
HALF_X, MID_Y = LENGTH / 2, WIDTH / 2
GOAL_HALF, GOAL_DEPTH = 3.66, 2.0
BOX_DEPTH, BOX_HALF = 16.5, 20.16
SIX_DEPTH, SIX_HALF = 5.5, 9.16
SPOT_DIST, CIRCLE_R, CORNER_R = 11.0, 9.15, 1.0
PAD = 3.0  # metres of surround drawn outside the touchlines
BALL_NUDGE = 1.0  # metres between the actor and the drawn ball

THEMES = {
    "dark": {
        "paper": "#0b1220", "grass": "#123024", "line": "rgba(236,242,238,0.62)", "text": "#e6ebf0", "muted": "#94a3b8",
        "home": "#3d8bfd", "away": "#f2545b", "number": "#ffffff", "actor": "#fde047", "ball": "#ffffff", "ball_edge": "#0b1220",
        "path": "rgba(255,255,255,0.85)", "offside": "#facc15", "font": "Inter, Segoe UI, system-ui, sans-serif",
    },
    "light": {
        "paper": "#ffffff", "grass": "#e7f2ea", "line": "#7d9384", "text": "#111827", "muted": "#4b5563",
        "home": "#1d5fd1", "away": "#d62f37", "number": "#ffffff", "actor": "#b45309", "ball": "#ffffff", "ball_edge": "#111827",
        "path": "rgba(17,24,39,0.8)", "offside": "#b45309", "font": "Inter, Segoe UI, system-ui, sans-serif",
    },
}

ACTION_LABELS = {"freekick_short": "short free kick", "freekick_crossed": "crossed free kick", "corner_short": "short corner", "corner_crossed": "corner", "shot_freekick": "free-kick shot", "keeper_save": "save", "keeper_claim": "claim", "goalkick": "goal kick", "throw_in": "throw-in", "take_on": "take-on"}

# trace order shared by every frame figure, so that animation frames update the same traces
TRACE_HOME, TRACE_AWAY, TRACE_ACTOR, TRACE_PATH, TRACE_BALL = range(5)


def _theme(theme: str | dict) -> dict:
    return theme if isinstance(theme, dict) else THEMES[theme]


def _arc(cx: float, cy: float, r: float, a0: float, a1: float, n: int = 40) -> str:
    pts = [(cx + r * math.cos(a0 + (a1 - a0) * i / n), cy + r * math.sin(a0 + (a1 - a0) * i / n)) for i in range(n + 1)]
    return "M " + " L ".join(f"{x:.3f},{y:.3f}" for x, y in pts)


def pitch_shapes(theme: str | dict = "dark") -> list[dict]:
    """The pitch markings as plotly layout shapes, in metres."""
    t = _theme(theme)
    line = {"color": t["line"], "width": 1.6}
    shapes = [
        {"type": "rect", "x0": -PAD, "y0": -PAD, "x1": LENGTH + PAD, "y1": WIDTH + PAD, "fillcolor": t["grass"], "line": {"width": 0}, "layer": "below"},
        {"type": "rect", "x0": 0, "y0": 0, "x1": LENGTH, "y1": WIDTH, "line": line, "layer": "below"},
        {"type": "line", "x0": HALF_X, "y0": 0, "x1": HALF_X, "y1": WIDTH, "line": line, "layer": "below"},
        {"type": "circle", "x0": HALF_X - CIRCLE_R, "y0": MID_Y - CIRCLE_R, "x1": HALF_X + CIRCLE_R, "y1": MID_Y + CIRCLE_R, "line": line, "layer": "below"},
    ]
    spot = 0.15
    shapes.append({"type": "circle", "x0": HALF_X - spot, "y0": MID_Y - spot, "x1": HALF_X + spot, "y1": MID_Y + spot, "fillcolor": t["line"], "line": {"width": 0}, "layer": "below"})
    arc_half = math.acos((BOX_DEPTH - SPOT_DIST) / CIRCLE_R)  # the part of the penalty arc outside the box
    for goal_x, sign in ((0.0, 1), (LENGTH, -1)):
        box_x, six_x, spot_x = goal_x + sign * BOX_DEPTH, goal_x + sign * SIX_DEPTH, goal_x + sign * SPOT_DIST
        shapes += [
            {"type": "rect", "x0": goal_x, "y0": MID_Y - BOX_HALF, "x1": box_x, "y1": MID_Y + BOX_HALF, "line": line, "layer": "below"},
            {"type": "rect", "x0": goal_x, "y0": MID_Y - SIX_HALF, "x1": six_x, "y1": MID_Y + SIX_HALF, "line": line, "layer": "below"},
            {"type": "rect", "x0": goal_x, "y0": MID_Y - GOAL_HALF, "x1": goal_x - sign * GOAL_DEPTH, "y1": MID_Y + GOAL_HALF, "line": {"color": t["line"], "width": 2.4}, "fillcolor": "rgba(255,255,255,0.08)", "layer": "below"},
            {"type": "circle", "x0": spot_x - spot, "y0": MID_Y - spot, "x1": spot_x + spot, "y1": MID_Y + spot, "fillcolor": t["line"], "line": {"width": 0}, "layer": "below"},
        ]
        centre_angle = 0.0 if sign == 1 else math.pi
        shapes.append({"type": "path", "path": _arc(spot_x, MID_Y, CIRCLE_R, centre_angle - arc_half, centre_angle + arc_half), "line": line, "layer": "below"})
    for cx, cy, a0 in ((0, 0, 0), (LENGTH, 0, math.pi / 2), (LENGTH, WIDTH, math.pi), (0, WIDTH, 3 * math.pi / 2)):
        shapes.append({"type": "path", "path": _arc(cx, cy, CORNER_R, a0, a0 + math.pi / 2, 10), "line": line, "layer": "below"})
    return shapes


def pitch_figure(theme: str | dict = "dark", width: int | None = 900, x_range: tuple[float, float] | None = None, y_range: tuple[float, float] | None = None) -> go.Figure:
    """An empty 105 x 68 m pitch. The height follows the width so that one metre is as long on both axes."""
    t = _theme(theme)
    xr = x_range or (-PAD - 1, LENGTH + PAD + 1)
    yr = y_range or (-PAD - 1, WIDTH + PAD + 1)
    margin = {"l": 10, "r": 10, "t": 64, "b": 10}
    layout = {
        "shapes": pitch_shapes(t),
        "xaxis": {"range": list(xr), "visible": False, "fixedrange": True, "constrain": "domain"},
        "yaxis": {"range": list(yr), "visible": False, "fixedrange": True, "scaleanchor": "x", "scaleratio": 1, "constrain": "domain"},
        "paper_bgcolor": t["paper"], "plot_bgcolor": t["paper"],
        "font": {"family": t["font"], "color": t["text"], "size": 13},
        "margin": margin, "showlegend": False, "hovermode": "closest",
        "title": {"x": 0.02, "xanchor": "left", "y": 0.97, "yanchor": "top", "font": {"size": 17}},
    }
    if width:
        plot_w = width - margin["l"] - margin["r"]
        layout.update(width=width, height=round(plot_w * (yr[1] - yr[0]) / (xr[1] - xr[0])) + margin["t"] + margin["b"])
    return go.Figure(layout=layout)


# --- frames ---------------------------------------------------------------------------------


def side_of(player_id: str, teammate: bool, acting_side: str) -> str:
    """'home' or 'away' for a freeze-frame entry: from the id prefix when there is one, else from the teammate flag."""
    prefix = str(player_id).split("-", 1)[0]
    if prefix in ("home", "away"):
        return prefix
    return acting_side if teammate else ("away" if acting_side == "home" else "home")


def shirt(player_id: str) -> str:
    m = re.search(r"(\d+)$", str(player_id))
    return m.group(1) if m else ""


def clamp(x: float, y: float) -> tuple[float, float]:
    """Keeps a drawn point on the pitch (a ball path can end a little beyond a line)."""
    return min(max(x, 0.0), LENGTH), min(max(y, 0.0), WIDTH)


def _viewer(action: dict, view: str | None):
    """Function from the acting team's frame to the drawn frame: `view` is the team drawn attacking towards x = 105."""
    flip = view is not None and view != action["team"]
    return (lambda x, y: (LENGTH - x, WIDTH - y)) if flip else (lambda x, y: (x, y))


def _label(action: dict) -> str:
    kind = ACTION_LABELS.get(action["action_type"], action["action_type"].replace("_", " "))
    if action.get("technique"):
        kind += f" ({action['technique'].replace('_', ' ')})"
    p = action.get("player") or {}
    who = f"{action.get('team_name', action['team'].title())} #{p.get('shirt', shirt(p.get('id', '')))} {p.get('name', '')}".strip()
    shot = action.get("shot")
    extra = f", {shot['outcome']}, xG {shot['xg']:.2f}" if shot else ""
    return f"{who}, {kind}{extra}"


def frame_title(action: dict, home_name: str, away_name: str) -> str:
    s = action.get("score", {"home": 0, "away": 0})
    head = f"<b>{action.get('minute_label', '')}  {home_name} {s['home']}-{s['away']} {away_name}</b>"
    return f"{head}<br><span style='font-size:13px'>{_label(action)}</span>"


def frame_traces(action: dict, theme: str | dict = "dark", view: str | None = None, names: dict | None = None, scale: float = 1.0) -> list[go.Scatter]:
    """The five traces of an action (home players, away players, actor ring, action path, ball), always in that order. `scale` enlarges the markers (for zoomed views)."""
    t = _theme(theme)
    to_view = _viewer(action, view)
    names = names or {}
    teams = {"home": [], "away": []}
    actor = None
    for p in action.get("freeze_frame") or []:
        side = side_of(p["player_id"], p["teammate"], action["team"])
        x, y = clamp(*to_view(*p["location"]))
        teams[side].append((p, x, y))
        if p.get("actor"):
            actor = (x, y)
    traces = []
    for side in ("home", "away"):
        rows = sorted(teams[side], key=lambda r: (not r[0].get("keeper"), int(shirt(r[0]["player_id"]) or 0)))
        traces.append(go.Scatter(
            x=[r[1] for r in rows], y=[r[2] for r in rows], ids=[r[0]["player_id"] for r in rows], name=side,
            mode="markers+text", text=[shirt(r[0]["player_id"]) for r in rows], textposition="middle center",
            textfont={"color": t["number"], "size": round(10 * scale), "family": t["font"]},
            marker={"size": 19 * scale, "color": t[side], "symbol": ["square" if r[0].get("keeper") else "circle" for r in rows], "line": {"color": t["paper"], "width": 1.2}},
            hovertext=[f"{names.get(r[0]['player_id'], r[0]['player_id'])}{' (GK)' if r[0].get('keeper') else ''}" for r in rows], hoverinfo="text",
        ))
    if actor is None and action.get("start_x") is not None:
        actor = clamp(*to_view(action["start_x"], action["start_y"]))
    traces.append(go.Scatter(x=[actor[0]] if actor else [], y=[actor[1]] if actor else [], name="actor", mode="markers", hoverinfo="skip",
                             marker={"size": 30 * scale, "color": "rgba(0,0,0,0)", "line": {"color": t["actor"], "width": 3}}))
    sx, sy = clamp(*to_view(action["start_x"], action["start_y"]))
    ex, ey = clamp(*to_view(action["end_x"], action["end_y"]))
    moved = math.hypot(ex - sx, ey - sy) >= 0.5
    traces.append(go.Scatter(
        x=[sx, ex] if moved else [], y=[sy, ey] if moved else [], name="path", mode="lines+markers", hoverinfo="skip",
        line={"color": t["path"], "width": 2.2, "dash": "dot" if action["action_type"] in ("dribble", "take_on") else "solid"},
        marker={"symbol": ["circle", "arrow"], "size": [1, 13], "angleref": "previous", "color": t["path"]},
    ))
    # the ball at the actor's feet: nudged 1 m along the action (or forwards) so that it does not hide the shirt number
    ux, uy = ((ex - sx) / math.hypot(ex - sx, ey - sy), (ey - sy) / math.hypot(ex - sx, ey - sy)) if moved else (1.0, 0.0)
    bx, by = clamp(sx + BALL_NUDGE * ux, sy + BALL_NUDGE * uy)
    traces.append(go.Scatter(x=[bx], y=[by], customdata=[[sx, sy]], name="ball", mode="markers", hoverinfo="skip",
                             marker={"size": 9, "color": t["ball"], "line": {"color": t["ball_edge"], "width": 1.5}}))
    return traces


def frame_figure(action: dict, home_name: str = "Home", away_name: str = "Away", theme: str | dict = "dark", view: str | None = None,
                 names: dict | None = None, title: str | None = None, width: int | None = 900) -> go.Figure:
    """The freeze frame of one action: team colours, keepers as squares, the actor ringed, the ball at the start and an arrow to the end.

    `view` is the team drawn attacking towards x = 105 ("home" or "away"); by default the acting team, as in the feed. `names` maps player ids to names for the hover text.
    """
    fig = pitch_figure(theme, width)
    fig.add_traces(frame_traces(action, theme, view, names))
    fig.update_layout(title_text=title or frame_title(action, home_name, away_name))
    return fig


# --- offside --------------------------------------------------------------------------------


def offside_line(frame: list[dict]) -> tuple[float, str | None]:
    """The offside line in the attacking team's frame, as the generator computes it: the second-last defender, goalkeeper included."""
    defenders = sorted(((p["location"][0], p["player_id"]) for p in frame if not p["teammate"]), reverse=True)
    return (defenders[1][0], defenders[1][1]) if len(defenders) > 1 else (HALF_X, None)


def offside_geometry(pass_action: dict, runner_id: str | None = None) -> dict:
    """Line, runner and margin of a pass, in the passing team's frame.

    The runner is `runner_id`, else the player named by the action's `offside` field (added by the VAR correction), else the most advanced outfield team-mate of the passer. `margin_m` is the official margin carried by the feed when it has one; `frame_margin_m` is what the frame shows (its locations are rounded to 0.1 m, so the two can differ by up to 0.1 m).
    """
    frame = pass_action["freeze_frame"]
    official = pass_action.get("offside") or {}
    runner_id = runner_id or official.get("player_id")
    mates = [p for p in frame if p["teammate"] and not p.get("actor") and not p.get("keeper")]
    runner = next((p for p in mates if p["player_id"] == runner_id), None) if runner_id else max(mates, key=lambda p: p["location"][0])
    if runner is None:
        raise ValueError(f"{runner_id} is not in the freeze frame")
    line_x, defender = offside_line(frame)
    defender_y = next((p["location"][1] for p in frame if p["player_id"] == defender), runner["location"][1])
    frame_margin = runner["location"][0] - line_x
    margin = official.get("margin_m", frame_margin)
    return {
        "line_x": line_x, "defender_id": defender, "defender_y": defender_y, "runner_id": runner["player_id"], "runner_x": runner["location"][0], "runner_y": runner["location"][1],
        "frame_margin_m": round(frame_margin, 2), "margin_m": round(margin, 2), "official": "margin_m" in official,
    }


MARGIN_KEYS = ("margin_m", "frame_margin_m")  # what `show_margin=False` leaves out of the figure's meta


def offside_figure(pass_action: dict, defending_line: bool = True, home_name: str = "Home", away_name: str = "Away", theme: str | dict = "dark",
                   runner_id: str | None = None, zoom: float | None = None, names: dict | None = None, width: int | None = 900, show_margin: bool = True) -> go.Figure:
    """The frame of a pass, drawn in the passing team's frame, with the runner's margin over the offside line annotated in metres.

    `defending_line` draws the offside line across the pitch through the second-last defender, who is ringed. `zoom` is the half-length in metres of a window centred on the runner and the second-last defender (None: the whole pitch). `show_margin=False` draws no margin (no bracket, no metres in the annotation, the title or the meta), for anyone but the producer: the line, the runner and the defender stay.
    """
    t = _theme(theme)
    g = offside_geometry(pass_action, runner_id)
    if zoom:
        half_y = zoom * 0.75
        scale = 1.7
        dy = g["defender_y"]
        cy = (g["runner_y"] + dy) / 2 if abs(g["runner_y"] - dy) < 1.6 * half_y else g["runner_y"]
        cx = min(max((g["runner_x"] + g["line_x"]) / 2, zoom - PAD), LENGTH + PAD - zoom)
        cy = min(max(cy, half_y - PAD), WIDTH + PAD - half_y)
        fig = pitch_figure(theme, width, (cx - zoom, cx + zoom), (cy - half_y, cy + half_y))
    else:
        fig, scale = pitch_figure(theme, width), 1.0
    fig.add_traces(frame_traces(pass_action, theme, None, names, scale))
    side = pass_action["team"]
    other = "away" if side == "home" else "home"
    team_name = {"home": home_name, "away": away_name}
    fig.add_trace(go.Scatter(x=[g["runner_x"]], y=[g["runner_y"]], name="runner", mode="markers", hoverinfo="skip",
                             marker={"size": 30 * scale, "color": "rgba(0,0,0,0)", "line": {"color": t["offside"], "width": 3}}))
    if defending_line:
        fig.add_shape(type="line", x0=g["line_x"], x1=g["line_x"], y0=0, y1=WIDTH, line={"color": t["offside"], "width": 2, "dash": "dash"})
        if g["defender_id"]:
            fig.add_trace(go.Scatter(x=[g["line_x"]], y=[g["defender_y"]], name="second-last defender", mode="markers", hoverinfo="skip",
                                     marker={"size": 30 * scale, "color": "rgba(0,0,0,0)", "line": {"color": t["offside"], "width": 2, "dash": "dot"}}))
    meta = {k: v for k, v in g.items() if k not in MARGIN_KEYS}
    if show_margin:
        # the margin: a bracket from the line to the runner, beside him on the side away from the pass
        sx, sy, ex, ey = (pass_action[k] for k in ("start_x", "start_y", "end_x", "end_y"))
        path_y = sy + (ey - sy) * (g["runner_x"] - sx) / (ex - sx) if abs(ex - sx) > 0.5 else sy
        toward = 1 if g["runner_y"] >= path_y else -1
        by = min(max(g["runner_y"] + toward * 1.4, -PAD + 1), WIDTH + PAD - 1)
        tick = 0.6
        for x0, y0, x1, y1 in ((g["line_x"], by, g["runner_x"], by), (g["line_x"], by - tick, g["line_x"], by + tick), (g["runner_x"], by - tick, g["runner_x"], by + tick)):
            fig.add_shape(type="line", x0=x0, y0=y0, x1=x1, y1=y1, line={"color": t["offside"], "width": 2})
        margin_text = f"{g['margin_m']:.2f} m"
        fig.add_annotation(x=max(g["line_x"], g["runner_x"]), y=by, text=f"<b>{margin_text}</b>", showarrow=True, arrowhead=0, arrowcolor=t["offside"],
                           ax=34, ay=-30 * toward, font={"color": t["offside"], "size": 16}, name="margin")
        meta = {**g, "annotated_m": float(margin_text.split()[0])}
    defender = f" ({team_name[other]} #{shirt(g['defender_id'])})" if g["defender_id"] else ""
    beyond = "beyond" if g["margin_m"] > 0 else "behind"
    sub = f"{team_name[side]} #{shirt(g['runner_id'])} " + (f"{abs(g['margin_m']):.2f} m " if show_margin else "") + f"{beyond} the second-last defender{defender}"
    s = pass_action.get("score", {"home": 0, "away": 0})
    fig.update_layout(
        title_text=f"<b>{pass_action.get('minute_label', '')}  {home_name} {s['home']}-{s['away']} {away_name}</b><br><span style='font-size:13px'>{sub}</span>",
        meta={"offside": meta},
    )
    return fig


# --- sequences ------------------------------------------------------------------------------


def buildup(actions: list[dict], event_id: str, n_min: int = 5, n_max: int = 8) -> list[dict]:
    """The actions leading to `event_id` (included): its possession, cut to the last `n_max`, extended back to `n_min` if it is shorter."""
    ordered = sorted(actions, key=lambda a: a["seq"])
    i = next(k for k, a in enumerate(ordered) if a["event_id"] == event_id)
    j = i
    while j > 0 and i - j + 1 < n_max and ordered[j - 1].get("possession") == ordered[i].get("possession"):
        j -= 1
    j = min(j, max(i - n_min + 1, 0))
    return ordered[j:i + 1]


CONTROLS = {  # play button and step slider colours: light text on a dark button (or the reverse), the slider grip in the actor's colour
    "dark": {"button": "#1e293b", "button_text": "#f8fafc", "border": "#94a3b8", "grip": "#facc15", "tick": "#94a3b8", "label": "#e6ebf0"},
    "light": {"button": "#f1f5f9", "button_text": "#111827", "border": "#4b5563", "grip": "#b45309", "tick": "#4b5563", "label": "#111827"},
}


def play_controls(theme: str | dict, labels: list[str], play: dict, still: dict, frame_names: list[str] | None = None) -> dict:
    """Layout `updatemenus` and `sliders` for an animation: Play and Pause buttons that stay readable (no active highlight, which plotly draws near white under light text) and a slider whose steps carry `labels`."""
    t = _theme(theme)
    c = CONTROLS["light" if t.get("paper", "").lower() in ("#ffffff", "#fff", "white") else "dark"]
    names = frame_names or [str(k) for k in range(len(labels))]
    return {
        "updatemenus": [{"type": "buttons", "direction": "left", "x": 0.0, "y": -0.02, "xanchor": "left", "yanchor": "top", "pad": {"t": 6, "r": 6}, "showactive": False,
                         "bgcolor": c["button"], "bordercolor": c["border"], "borderwidth": 1, "font": {"color": c["button_text"], "size": 13},
                         "buttons": [{"label": "▶ Play", "method": "animate", "args": [None, play]}, {"label": "❚❚ Pause", "method": "animate", "args": [[None], still]}]}],
        "sliders": [{"x": 0.24, "y": -0.02, "len": 0.66, "xanchor": "left", "yanchor": "top", "pad": {"t": 8}, "currentvalue": {"visible": False},
                     "font": {"color": c["label"], "size": 12}, "bgcolor": c["grip"], "activebgcolor": c["label"], "bordercolor": c["border"], "borderwidth": 1,
                     "tickcolor": c["tick"], "ticklen": 5,
                     "steps": [{"label": label, "method": "animate", "args": [[name], still]} for label, name in zip(labels, names)]}],
    }


def sequence_frames(actions: list[dict], home_name: str = "Home", away_name: str = "Away", theme: str | dict = "dark", view: str | None = None,
                    names: dict | None = None) -> list[go.Frame]:
    """One plotly frame per action, all drawn with the same team attacking towards x = 105 (`view`, by default the team of the last action), so that players glide from one frame to the next."""
    view = view or actions[-1]["team"]
    return [
        go.Frame(name=str(k), data=frame_traces(a, theme, view, names), traces=list(range(5)), layout={"title": {"text": frame_title(a, home_name, away_name)}})
        for k, a in enumerate(actions)
    ]


def sequence_figure(actions: list[dict], home_name: str = "Home", away_name: str = "Away", theme: str | dict = "dark", view: str | None = None,
                    names: dict | None = None, duration_ms: int = 1100, width: int | None = 900) -> go.Figure:
    """The build-up as an animated figure with play and pause buttons and a slider."""
    frames = sequence_frames(actions, home_name, away_name, theme, view, names)
    fig = pitch_figure(theme, width)
    fig.add_traces(frames[0].data)
    fig.frames = frames
    play = {"frame": {"duration": duration_ms, "redraw": True}, "transition": {"duration": int(duration_ms * 0.6), "easing": "cubic-in-out"}, "fromcurrent": True}
    still = {"frame": {"duration": 0, "redraw": True}, "transition": {"duration": 0}, "mode": "immediate"}
    fig.update_layout(
        title_text=frames[0].layout.title.text,
        margin={"b": 70},
        **play_controls(theme, [str(k + 1) for k in range(len(actions))], play, still),
    )
    if width:
        fig.update_layout(height=fig.layout.height + 60)
    return fig
