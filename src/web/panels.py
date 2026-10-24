"""The console's match-graphics tab and its insights and storyline tab.

Graphics are drawn from the feed (`Store.feed`) with `web.graphics`; tactical details come from `worker.tactics` (pure functions, no model). Insight and storyline documents are read from the store; approve and reject go through `Store.decide_doc`, which uses the desk's own methods for them when it has some and degrades to a message when it has none. When the desk holds no insight yet, the panel lists the tactical candidates computed from the feed, marked as such, with nothing to approve. Like `console`, the functions below are plain functions that `register` wires as callbacks.
"""

import hashlib
import json

import plotly.graph_objects as go
from dash import Input, Output, State, ctx, dcc, html, no_update

from web import graphics as G
from web import pitch, security
from web.matchfeed import player_names, team_names
from web.store import get_store
from worker import tactics as T

PANEL_REFRESH_MS = 5000  # the desk ticks every 1.5 s (about 80 requests a minute); the visible panel adds 12, under the 120 per minute rate limit
THEME = dict(pitch.THEMES["dark"], paper="#1a1a19")
KINDS = [
    {"value": "shots", "label": "Shot map"},
    {"value": "momentum", "label": "Momentum and win probability"},
    {"value": "buildups", "label": "Goal and big-chance build-ups"},
    {"value": "setpieces", "label": "Corners and free kicks"},
    {"value": "direct", "label": "Penalties and direct free kicks"},
    {"value": "tactics", "label": "Tactical details"},
]
STATUS = {"proposed": "Writing", "pending": "Pending", "approved": "On air", "rejected": "Rejected", "superseded": "Superseded", "candidate": "Candidate"}
STATUS_CLASS = {"proposed": "st-proposed", "pending": "st-pending", "approved": "st-approved", "rejected": "st-rejected", "superseded": "st-retracted", "candidate": "st-proposed"}


def _sig(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def empty_figure(message: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper", font={"color": THEME["muted"], "size": 14})
    fig.update_layout(paper_bgcolor=THEME["paper"], plot_bgcolor=THEME["paper"], xaxis={"visible": False}, yaxis={"visible": False}, height=320, margin={"l": 10, "r": 10, "t": 10, "b": 10})
    return fig


def _sized(fig: go.Figure, height: int = 620) -> go.Figure:
    """Console figures follow the column width; pitch figures keep their 1:1 metres through scaleanchor."""
    fig.update_layout(width=None, height=height, autosize=True)
    return fig


def duo_block(doc: dict, lang: str | None = None) -> html.Div | None:
    """The commentary duo lines of a card or insight (`duo: [{speaker, lang, line}]`), grouped by language."""
    lines = [d for d in doc.get("duo") or [] if isinstance(d, dict) and (lang is None or d.get("lang") == lang)]
    if not lines:
        return None
    by_lang: dict[str, list[dict]] = {}
    for d in lines:
        by_lang.setdefault(d.get("lang", "en"), []).append(d)
    return html.Div([html.Div([html.Div(code.upper(), className="lang"),
                               *[html.P([html.Span(f"{d.get('speaker', '?')}: ", className="duo-speaker"), d.get("line", "")], className="duo-line") for d in items]],
                              className="duo-lang") for code, items in by_lang.items()], className="duo")


def facts_table(facts: dict, meanings: dict) -> html.Table:
    return html.Table([html.Tbody([html.Tr([html.Td(k, className="fact-key"), html.Td(str(v)), html.Td(meanings.get(k, ""), className="muted small")]) for k, v in facts.items()])],
                      className="facts")


def texts_block(doc: dict) -> list:
    mt = set(doc.get("machine_translated") or [])
    return [html.Div([html.Div([html.Span(t.get("lang", "").upper(), className="lang"), *([html.Span("machine-translated", className="badge st-mt")] if t.get("lang") in mt else [])],
                               className="lang-row"),
                      html.Div(t.get("headline", ""), className="headline"), html.Div(t.get("why", ""), className="why")], className="text-block")
            for t in doc.get("texts") or []]


# --- match graphics ---------------------------------------------------------------------------------

def graphic_items(feed: dict, kind: str) -> list[dict]:
    names = player_names(feed.get("info"))
    teams = team_names(feed.get("info"))
    who = lambda pid: names.get(pid, pid)  # noqa: E731
    if kind == "buildups":
        return [{"value": a["event_id"], "label": f"{a.get('minute_label', '')} {teams[a['team']]} {'goal' if T.any_goal(a) else 'chance'}" + (" (ruled out)" if a.get("voided") else "")
                 + f", {G._who(a)}, xG {a['shot']['xg']:.2f}"} for a in G.moments(feed)]
    if kind == "setpieces":
        out = [{"value": f"map:{s}", "label": f"All {teams[s]} deliveries"} for s in ("home", "away")]
        return out + [{"value": f"sp:{a['event_id']}", "label": f"{a.get('minute_label', '')} {teams[a['team']]} {pitch.ACTION_LABELS.get(a['action_type'], a['action_type'])}, {G._who(a)}"}
                      for a in feed["actions"] if a["action_type"] in G.SET_PIECES]
    if kind == "direct":
        return [{"value": a["event_id"], "label": f"{a.get('minute_label', '')} {teams[a['team']]} {'penalty' if a['action_type'] == 'shot_penalty' else 'free kick'}, {G._who(a)}"}
                for a in feed["actions"] if a["action_type"] in G.DIRECT]
    if kind == "tactics":
        out = [{"value": "lines", "label": "Defensive line height over time"}, {"value": "press", "label": "Pressing intensity over time"}]
        breaks = sorted(T.line_breaks(feed["actions"], 4), key=lambda r: (-r["bypassed"], r["seq"]))[:8]
        out += [{"value": f"lb:{r['event_id']}", "label": f"Line break {r['minute_label']}: {who(r['passer_id'])}, {r['bypassed']} defenders bypassed"} for r in breaks]
        runs = sorted(T.runs_behind(feed["actions"]), key=lambda r: abs(r["margin_m"]))[:8]
        out += [{"value": f"run:{r['event_id']}:{r['runner_id']}", "label": f"Run {r['minute_label']}: {who(r['runner_id'])} {abs(r['margin_m']):.2f} m {'beyond' if r['margin_m'] > 0 else 'inside'} the line"}
                for r in runs]
        spaces = sorted(T.space_at_reception(feed["actions"]), key=lambda r: -r["space_m"])[:6]
        out += [{"value": f"space:{r['event_id']}", "label": f"Space {r['minute_label']}: {who(r['receiver_id'])} {r['space_m']:.1f} m free"} for r in spaces]
        return out
    return []


def graphic_figure(feed: dict, kind: str, item: str | None, timeline: list[dict] | None = None) -> go.Figure:
    if kind == "shots":
        return _sized(G.shot_map(feed, THEME, None), 600)
    if kind == "momentum":
        return _sized(G.momentum_figure(feed, timeline, THEME, width=None), 600 if timeline else 380)
    if not item:
        return empty_figure("Nothing of this kind in the match so far.")
    if kind == "buildups":
        return _sized(G.buildup_figure(feed, item, THEME, None))
    if kind == "setpieces":
        tag, _, rest = item.partition(":")
        return _sized(G.set_pieces_map(feed, rest, THEME, None) if tag == "map" else G.set_piece_figure(feed, rest, THEME, None), 640)
    if kind == "direct":
        return _sized(G.shot_on_goal_figure(feed, item, THEME, None), 360)
    if kind == "tactics":
        tag, _, rest = item.partition(":")
        if tag == "lines":
            return _sized(G.line_height_figure(feed, THEME, width=None), 400)
        if tag == "press":
            return _sized(G.pressing_figure(feed, THEME, width=None), 400)
        if tag == "lb":
            return _sized(G.line_break_figure(feed, rest, THEME, None))
        if tag == "run":
            event_id, _, runner = rest.partition(":")
            return _sized(G.run_figure(feed, event_id, runner or None, THEME, width=None))
        if tag == "space":
            return _sized(G.space_figure(feed, rest, THEME, None))
    return empty_figure("Pick a graphic.")


NOTES = {
    "shots": "Marker size is the xG of the shot. Purple crosses are goals VAR ruled out.",
    "momentum": "Threat added: value of where completed passes and carries ended minus where they started (value = xG of a shot from there). Win probability from the desk's match state.",
    "buildups": "The possession behind the goal or chance (last 10 actions). Arrow width: threat added; the shot arrow: xG.",
    "setpieces": "Delivery width: value of the landing point. Play the animation: delivery, first contact, what followed.",
    "direct": "Seen from behind the taker. The box shows where the keeper stood in the frame.",
    "tactics": "Measured on the 360 freeze frames (synthetic positions, rounded to 0.1 m).",
}


def graphics(match_id: str | None, kind: str | None, item: str | None, view: str | None, last_sig: str | None, store=None):
    """(item options, item value, figure, note, signature); no_update everywhere when the tab is hidden or nothing changed."""
    if view != "graphics":
        return (no_update,) * 5
    store = store or get_store()
    kind = kind or "shots"
    if not match_id:
        return [], None, empty_figure("No match on the desk yet."), "", None
    feed = store.feed(match_id)
    if feed is None or not feed["actions"]:
        return [], None, empty_figure("No feed for this match: set REPLAY_DIR, or put feed.jsonl.gz in the desk folder."), "", None
    state = store.state(match_id) or {}
    options = graphic_items(feed, kind)
    values = [o["value"] for o in options]
    picked = item if item in values else (values[-1] if kind in ("buildups",) and values else values[0] if values else None)
    sig = _sig(match_id, kind, picked, feed["last_seq"], state.get("last_seq") if kind == "momentum" else None)
    if sig == last_sig:
        return (no_update,) * 5
    try:
        fig = graphic_figure(feed, kind, picked, state.get("wp_timeline") if kind == "momentum" else None)
    except (StopIteration, KeyError, ValueError, IndexError) as error:
        fig = empty_figure(f"Cannot draw this graphic: {error}")
    return options, picked if picked != item else no_update, fig, NOTES.get(kind, ""), sig


def graphics_pane() -> html.Section:
    return html.Section([
        html.Div([dcc.Dropdown(id="c-g-kind", options=KINDS, value="shots", clearable=False, className="match-picker"),
                  dcc.Dropdown(id="c-g-item", options=[], clearable=False, className="match-picker g-item", placeholder="Moment")], className="g-controls"),
        dcc.Graph(id="c-g-figure", figure=empty_figure("Loading."), config={"displayModeBar": False}, className="g-figure"),
        html.P(id="c-g-note", className="muted small"), dcc.Store(id="c-g-sig"), dcc.Interval(id="c-g-tick", interval=PANEL_REFRESH_MS, disabled=True)], id="c-pane-graphics", className="col pane", style={"display": "none"})


# --- insights and storyline ---------------------------------------------------------------------------

def insight_rows(store, match_id: str) -> tuple[list[dict], bool]:
    """(insight documents, from_desk): the desk's insights, else the tactical candidates computed from the feed (status `candidate`)."""
    docs = store.insights(match_id)
    if docs:
        return docs, True
    feed = store.feed(match_id)
    if not feed or not feed["actions"]:
        return [], False
    return [dict(d, status="candidate") for d in T.insight_candidates(feed["actions"], feed["info"], match_id)], False


def insight_options(docs: list[dict]) -> list[dict]:
    return [{"value": d["id"], "label": html.Div([
        html.Div([html.Span(d.get("minute_label") or "", className="ru-minute"), html.Span((d.get("kind") or "").title(), className="ru-moment"),
                  html.Span(f"{d.get('value', 0)} pts", className="ru-value")], className="ru-top"),
        html.Div(insight_title(d), className="ru-who"),
        html.Div(html.Span(STATUS.get(d.get("status"), d.get("status", "?")), className="badge " + STATUS_CLASS.get(d.get("status"), "st-proposed")), className="ru-badges")],
        className="ru-item")} for d in docs]


def insight_title(d: dict) -> str:
    texts = d.get("texts") or []
    en = next((t for t in texts if t.get("lang") == "en"), texts[0] if texts else None)
    if en:
        return en.get("headline", "")
    f = d.get("headline_facts") or {}
    kind = (d.get("figure") or {}).get("kind")
    if kind == "line_break" and "defenders_bypassed" in f:
        return f"{f.get('passer_name', '')}: {f['defenders_bypassed']} defenders bypassed before the {f.get('goal_minute_label', '')} goal"
    if kind == "run" and "margin_m" in f:
        return f"{f.get('runner_name', '')}: {f['margin_m']} m {'beyond' if f.get('runner_beyond_line') else 'inside'} the line"
    if kind == "space" and "space_m" in f:
        return f"{f.get('scorer_name', '')}: {f['space_m']} m of space before the {f.get('goal_minute_label', '')} goal"
    if kind == "line_height":
        return f"{f.get('team_name', '')} defensive line {f.get('line_moved', 'moved')} by {f.get('change_m', '?')} m"
    if kind == "pressing":
        return f"{f.get('team_name', '')} pressed {f.get('pressed', '')} after the break"
    return (d.get("key") or d.get("id", "")).replace("-", " ")


def insight_detail(d: dict | None, feed: dict | None, from_desk: bool) -> tuple[html.Div, go.Figure]:
    if d is None:
        return html.Div("No insight yet. Tactical candidates appear here once the feed has goals to explain.", className="muted"), empty_figure("No insight selected.")
    rarity = d.get("rarity") or {}
    parts = [html.H2(insight_title(d)),
             html.Div([html.Span(f"{d.get('minute_label') or ''} {d.get('kind', '')}, value {d.get('value', 0)} pts", className="muted"),
                       html.Span(STATUS.get(d.get("status"), d.get("status", "?")), className="badge " + STATUS_CLASS.get(d.get("status"), "st-proposed"))], className="detail-meta")]
    if not from_desk:
        parts.append(html.P("Candidate computed from the feed by worker.tactics: not on the desk, no text yet, nothing to approve.", className="muted small"))
    parts += texts_block(d)
    duo = duo_block(d)
    if duo is not None:
        parts += [html.H3("Commentary"), duo]
    parts += [html.H3("Facts"), facts_table(d.get("headline_facts") or {}, d.get("field_meanings") or {})]
    if rarity.get("basis"):
        parts.append(html.P(f"Rarity: {rarity['basis']}" + (f" ({rarity['surprisal_bits']} bits)" if rarity.get("surprisal_bits") is not None else ""), className="muted small"))
    fig = G.insight_figure(d, feed, THEME, None) if feed else None
    return html.Div(parts), _sized(fig) if fig is not None else empty_figure("No figure for this insight.")


def storyline_block(doc: dict | None) -> html.Div:
    if not doc:
        return html.Div("No storyline for this match yet.", className="muted")
    parts = [html.Div([html.Span(STATUS.get(doc.get("status"), doc.get("status", "")), className="badge " + STATUS_CLASS.get(doc.get("status"), "st-proposed"))], className="detail-meta")]
    parts += texts_block(doc)
    duo = duo_block(doc)
    if duo is not None:
        parts += [html.H3("Commentary"), duo]
    parts += [html.H3("Facts"), facts_table(doc.get("facts") or doc.get("headline_facts") or {}, doc.get("field_meanings") or {})]
    return html.Div(parts)


def insights(match_id: str | None, selected: str | None, view: str | None, last_sig: str | None, rev: int | None = None, store=None):
    """(options, selected, detail, figure, storyline, approve disabled, reject disabled, storyline approve disabled, storyline reject disabled, signature)."""
    if view != "insights":
        return (no_update,) * 10
    store = store or get_store()
    if not match_id:
        return [], None, insight_detail(None, None, False)[0], empty_figure("No match."), storyline_block(None), True, True, True, True, None
    docs, from_desk = insight_rows(store, match_id)
    story = store.storyline(match_id)
    ids = [d["id"] for d in docs]
    picked = selected if selected in ids else (ids[0] if ids else None)
    feed = store.feed(match_id)
    sig = _sig(match_id, docs, story, picked, rev, (feed or {}).get("last_seq"))
    if sig == last_sig:
        return (no_update,) * 10
    doc = next((d for d in docs if d["id"] == picked), None)
    detail, fig = insight_detail(doc, feed, from_desk)
    status = (doc or {}).get("status")
    no_doc = not from_desk or doc is None
    s_status = (story or {}).get("status")
    return (insight_options(docs), picked if picked != selected else no_update, detail, fig, storyline_block(story),
            no_doc or status in ("approved", "superseded"), no_doc or status in ("rejected", "superseded"),
            story is None or s_status == "approved", story is None or s_status == "rejected", sig)


def decide_doc(action: str, match_id: str | None, doc_id: str | None, by: str | None, store=None) -> tuple[str, bool]:
    if by is None:
        return "Sign in to approve or reject.", False
    if not match_id or not doc_id:
        return "Select an insight first.", False
    store = store or get_store()
    try:
        store.decide_doc(match_id, doc_id, action, by)
    except KeyError:
        return "This document is not on the desk (a candidate cannot be approved).", False
    except NotImplementedError:
        return "The desk cannot decide insights or storylines yet.", False
    except (ValueError, TypeError, AttributeError) as error:
        return f"Refused: {error}.", False
    return ("On air" if action == "approve" else "Rejected") + f", by {by}.", True


def insights_pane() -> html.Div:
    return html.Div([
        html.Section([html.H3("Insights"), dcc.RadioItems(id="c-i-list", options=[], className="rundown", inputClassName="ru-radio", labelClassName="ru-label")], className="col col-left"),
        html.Section([html.Div(id="c-i-detail"),
                      html.Div([html.Button("Approve", id="c-i-approve", className="btn btn-approve", disabled=True),
                                html.Button("Reject", id="c-i-reject", className="btn btn-reject", disabled=True)], className="actions"),
                      html.Div(id="c-i-message", className="message", role="status"),
                      dcc.Graph(id="c-i-figure", figure=empty_figure("Loading."), config={"displayModeBar": False})], className="col col-middle"),
        html.Section([html.H3("Storyline"), html.Div(id="c-s-detail"),
                      html.Div([html.Button("Approve", id="c-s-approve", className="btn btn-approve", disabled=True),
                                html.Button("Reject", id="c-s-reject", className="btn btn-reject", disabled=True)], className="actions")], className="col col-right"),
        dcc.Store(id="c-i-sig"), dcc.Store(id="c-i-rev", data=0), dcc.Interval(id="c-i-tick", interval=PANEL_REFRESH_MS, disabled=True)], id="c-pane-insights", className="desk pane", style={"display": "none"})


# --- wiring ------------------------------------------------------------------------------------------

VIEWS = [{"value": "desk", "label": "Desk"}, {"value": "graphics", "label": "Match graphics"}, {"value": "insights", "label": "Insights and storyline"}]


def view_styles(view: str | None) -> tuple[dict, dict, dict, bool, bool]:
    """Pane styles, then whether the graphics and insights refresh timers are off: a hidden pane sends no request (the rate limit counts every callback)."""
    show, hide = {}, {"display": "none"}
    view = view or "desk"
    return (show if view == "desk" else hide, show if view == "graphics" else hide, show if view == "insights" else hide,
            view != "graphics", view != "insights")


def _graphics_cb(_n, match_id, kind, item, view, last_sig):
    if security.producer() is None:
        return [], None, empty_figure("Sign in to see the graphics."), "", None
    return graphics(match_id, kind, item, view, last_sig)


def _insights_cb(_n, rev, match_id, selected, view, last_sig):
    if security.producer() is None:
        return [], None, html.Div("Sign in."), empty_figure(""), "", True, True, True, True, None
    return insights(match_id, selected, view, last_sig, rev)


def _decide_cb(_a, _r, _sa, _sr, match_id, doc_id, rev):
    trig = ctx.triggered_id
    action = {"c-i-approve": "approve", "c-i-reject": "reject", "c-s-approve": "approve", "c-s-reject": "reject"}.get(trig)
    if action is None:
        return no_update, no_update
    if trig.startswith("c-s-"):
        doc_id = f"storyline-{match_id}" if match_id else None
    message, done = decide_doc(action, match_id, doc_id, security.producer())
    return html.Span(message, className="ok" if done else "error"), (rev or 0) + 1 if done else no_update


def register(app) -> None:
    app.callback(Output("c-pane-desk", "style"), Output("c-pane-graphics", "style"), Output("c-pane-insights", "style"), Output("c-g-tick", "disabled"), Output("c-i-tick", "disabled"),
                 Input("c-view", "value"))(view_styles)
    app.callback(Output("c-g-item", "options"), Output("c-g-item", "value"), Output("c-g-figure", "figure"), Output("c-g-note", "children"), Output("c-g-sig", "data"),
                 Input("c-g-tick", "n_intervals"), Input("c-match", "value"), Input("c-g-kind", "value"), Input("c-g-item", "value"), Input("c-view", "value"),
                 State("c-g-sig", "data"))(_graphics_cb)
    app.callback(Output("c-i-list", "options"), Output("c-i-list", "value"), Output("c-i-detail", "children"), Output("c-i-figure", "figure"), Output("c-s-detail", "children"),
                 Output("c-i-approve", "disabled"), Output("c-i-reject", "disabled"), Output("c-s-approve", "disabled"), Output("c-s-reject", "disabled"), Output("c-i-sig", "data"),
                 Input("c-i-tick", "n_intervals"), Input("c-i-rev", "data"), Input("c-match", "value"), Input("c-i-list", "value"), Input("c-view", "value"),
                 State("c-i-sig", "data"))(_insights_cb)
    app.callback(Output("c-i-message", "children"), Output("c-i-rev", "data"),
                 Input("c-i-approve", "n_clicks"), Input("c-i-reject", "n_clicks"), Input("c-s-approve", "n_clicks"), Input("c-s-reject", "n_clicks"),
                 State("c-match", "value"), State("c-i-list", "value"), State("c-i-rev", "data"), prevent_initial_call=True)(_decide_cb)
