"""The producer console (`/`): the rundown of cards, the selected card with its texts, facts and history, the producer's decisions, and the win-probability curve.

Every decision goes through `worker.desk.Desk` (via the store), which writes the audit trail; an edited text is checked by the Verifier before it goes on air. The functions below are plain functions that `register` wires as callbacks, so the tests call them directly.
"""

import hashlib
import json

import plotly.graph_objects as go
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update

from web import panels, security
from web.store import get_store

LANG_NAMES = {"en": "English", "fr": "French", "es": "Spanish"}
MOMENTS = {"goal": "Goal", "save": "Save", "red_card": "Red card", "chance": "Chance"}
STATUS = {"proposed": ("Writing", "st-proposed"), "pending": ("Pending", "st-pending"), "approved": ("On air", "st-approved"),
          "rejected": ("Rejected", "st-rejected"), "retracted": ("Retracted", "st-retracted")}
COLORS = {"home": "#3987e5", "draw": "#9a998f", "away": "#d95926", "muted": "#6f6e69", "ink": "#f0efec", "ink2": "#c3c2b7", "surface": "#1a1a19", "grid": "#2c2c2a"}


# --- pieces ---------------------------------------------------------------------------------------

def badges(card: dict) -> list:
    label, cls = STATUS.get(card.get("status"), (card.get("status", "?"), "st-proposed"))
    if card.get("status") == "retracted" and card.get("retracted_on_air"):
        label = "Retracted on air"
    out = [html.Span(label, className=f"badge {cls}")]
    if card.get("under_review"):
        out.append(html.Span("VAR check", className="badge st-var"))
    return out


def rundown_options(cards: list[dict]) -> list[dict]:
    return [{"value": c["id"], "label": html.Div([
        html.Div([html.Span(c.get("minute_label", ""), className="ru-minute"), html.Span(MOMENTS.get(c.get("moment"), c.get("moment")), className="ru-moment"),
                  html.Span(f"priority {c.get('value_pts', 0)}", className="ru-value")], className="ru-top"),
        html.Div(f"{c.get('team_name', '')}: {c.get('player', {}).get('name', '')}" + (f" (v{c['version']})" if c.get("version", 1) > 1 else ""), className="ru-who"),
        html.Div(badges(c), className="ru-badges")], className="ru-item " + ("ru-retracted" if c.get("status") == "retracted" else ""))} for c in cards]


def detail(card: dict | None, audit: list[dict]) -> html.Div:
    if card is None:
        return html.Div("Select a card in the rundown.", className="muted")
    mt = set(card.get("machine_translated") or [])
    texts = [html.Div([html.Div([html.Span(LANG_NAMES.get(t["lang"], t["lang"]), className="lang"),
                                 *([html.Span("machine-translated", className="badge st-mt")] if t["lang"] in mt else [])], className="lang-row"),
                       html.Div(t.get("headline", ""), className="headline"), html.Div(t.get("why", ""), className="why")], className="text-block")
             for t in card.get("texts") or []] or [html.Div("Not written yet.", className="muted")]
    facts = html.Table([html.Tbody([html.Tr([html.Td(k, className="fact-key"), html.Td(str(v))]) for k, v in (card.get("facts") or {}).items()])], className="facts")
    v = card.get("verification") or {}
    verification = html.Div(
        [html.Span("Verified" if v.get("verified") else "Not verified", className="badge " + ("st-approved" if v.get("verified") else "st-retracted")),
         html.Span(f" writer {card.get('writer', '?')}, {v.get('attempts', 0)} model attempt(s)", className="muted")]) if v else html.Div("Not checked yet.", className="muted")
    entries = [e for e in audit if e.get("card_id") == card["id"] and e.get("by")]  # desk entries; the Spotter's own lines repeat them
    history = html.Ol([html.Li([html.Span(e.get("action", ""), className="h-action"), html.Span(f" by {e['by']}"),
                                html.Span(f", v{e['version']}" if e.get("version") else ""), html.Span(f", {e['cause']}" if e.get("cause") else "", className="muted")])
                       for e in entries] or [html.Li(f"{h['action']} by {h['by']}, v{h['version']}") for h in card.get("history", [])], className="history")
    sb = card.get("score_before", {})
    head = html.Div([html.H2(f"{card.get('minute_label', '')} {MOMENTS.get(card.get('moment'), card.get('moment'))}: {card.get('player', {}).get('name', '')}"),
                     html.Div([html.Span(f"{card.get('team_name', '')}, score before {sb.get('home', 0)}-{sb.get('away', 0)}, priority {card.get('value_pts', 0)}, version {card.get('version', 1)}", className="muted"),
                               *badges(card)], className="detail-meta")])
    duo = panels.duo_block(card)
    commentary = [html.H3("Commentary"), duo] if duo is not None else []
    return html.Div([head, html.Div(texts), *commentary, html.H3("Facts"), facts, html.H3("Verification"), verification, html.H3("History"), history])


PERIOD_STARTS = (2700.0, 5400.0, 6300.0)  # second half, extra time


def axis(timeline: list[dict]) -> tuple[list[dict], dict[str, float], list[float]]:
    """Points in match order with a continuous x in minutes (added time pushes the next period right), x by event id, and where each period starts."""
    period, top, starts = 0, 0.0, [0.0]
    tagged = []
    for p in sorted(timeline, key=lambda p: p["seq"]):
        nxt = PERIOD_STARTS[period] if period < len(PERIOD_STARTS) else None
        if nxt is not None and p["clock_s"] < top - 60 and abs(p["clock_s"] - nxt) < 90:  # the clock went back to a kick-off: a new period
            period += 1
            starts.append(top - nxt + starts[-1])
        top = max(top, p["clock_s"])
        tagged.append((period, p))
    out = [dict(p, x=(p["clock_s"] + starts[k]) / 60) for k, p in sorted(tagged, key=lambda t: (t[0], t[1]["clock_s"], t[1]["seq"]))]
    breaks = [(PERIOD_STARTS[k - 1] + starts[k]) / 60 for k in range(1, len(starts))]
    return out, {p["event_id"]: p["x"] for p in out}, breaks


def figure(state: dict | None, cards: list[dict], selected: str | None = None) -> go.Figure:
    fig = go.Figure()
    teams = (state or {}).get("teams") or {"home": "Home", "away": "Away"}
    points, x_of, breaks = axis((state or {}).get("wp_timeline") or [])
    xs = [p["x"] for p in points]
    for key, name in (("home", f"{teams['home']} win"), ("draw", "Draw"), ("away", f"{teams['away']} win")):
        fig.add_trace(go.Scatter(x=xs, y=[p[key] * 100 for p in points], name=name, mode="lines", line={"color": COLORS[key], "width": 2},
                                 customdata=[p["minute_label"] for p in points], hovertemplate="%{customdata}  %{y:.0f}%<extra>" + name + "</extra>"))
    by_y = {p["event_id"]: p for p in points}
    groups = {"live": [], "retracted": []}
    for c in cards:
        p = by_y.get(c.get("event_id"))
        if p is None:
            continue
        groups["retracted" if c.get("status") == "retracted" else "live"].append((c, p))
    for kind, items in groups.items():
        if not items:
            continue
        retracted = kind == "retracted"
        fig.add_trace(go.Scatter(
            x=[p["x"] for _, p in items], y=[p[c["team"]] * 100 for c, p in items], mode="markers", name="Retracted cards" if retracted else "Cards",
            marker={"size": [16 if c["id"] == selected else 11 for c, _ in items], "symbol": "x-thin-open" if retracted else "circle",
                    "color": [COLORS["muted"] if retracted or c.get("status") == "rejected" else COLORS[c["team"]] for c, _ in items],
                    "line": {"width": [3 if c["id"] == selected else 2 for c, _ in items], "color": [COLORS["ink"] if c["id"] == selected else (COLORS["muted"] if retracted else COLORS["surface"]) for c, _ in items]},
                    "opacity": [1.0 if c.get("status") == "approved" or retracted else 0.55 for c, _ in items]},
            customdata=[c["id"] for c, _ in items],
            hovertext=[f"{c.get('minute_label', '')} {MOMENTS.get(c.get('moment'), '')} {c.get('team_name', '')}: {c.get('player', {}).get('name', '')} ({STATUS.get(c.get('status'), ('?',))[0].lower()})" for c, _ in items],
            hovertemplate="%{hovertext}<extra></extra>"))
    for b in breaks:
        fig.add_vline(x=b, line={"color": COLORS["muted"], "width": 1, "dash": "dot"})
    ticks = [m for m in (0, 15, 30, 45) if not breaks or m <= breaks[0]]
    tickvals = [float(m) for m in ticks]
    if breaks:
        shift = breaks[0] - 45
        tickvals += [m + shift for m in (60, 75, 90)]
        ticks += [60, 75, 90]
    fig.update_layout(
        template="plotly_dark", paper_bgcolor=COLORS["surface"], plot_bgcolor=COLORS["surface"], margin={"l": 44, "r": 12, "t": 12, "b": 36}, height=320,
        font={"color": COLORS["ink2"], "size": 12}, hovermode="closest", uirevision="wp",
        legend={"orientation": "h", "y": -0.18, "x": 0, "font": {"color": COLORS["ink2"]}},
        xaxis={"tickvals": tickvals, "ticktext": [f"{m}'" for m in ticks], "gridcolor": COLORS["grid"], "zeroline": False},
        yaxis={"range": [0, 100], "ticksuffix": "%", "gridcolor": COLORS["grid"], "zeroline": False})
    return fig


def scoreline(state: dict | None) -> str:
    if not state or not state.get("teams"):
        return "No match state yet"
    t, s = state["teams"], state.get("score", {})
    return f"{t['home']} {s.get('home', 0)}-{s.get('away', 0)} {t['away']}   {state.get('minute_label', '')}" + ("   full time" if state.get("final") else "")


def button_states(card: dict | None) -> tuple[bool, bool, bool]:
    """(approve, edit, reject) disabled."""
    if card is None:
        return True, True, True
    status = card.get("status")
    return status in ("retracted", "approved", "proposed"), status in ("retracted", "proposed"), status in ("retracted", "rejected", "proposed")


# --- callbacks as plain functions -----------------------------------------------------------------

def match_options(current: str | None, store=None) -> tuple[list[dict], str | None]:
    store = store or get_store()
    matches = store.matches()
    options = [{"value": m["match_id"], "label": f"{m['teams']['home']} v {m['teams']['away']} ({m['match_id']})" if m.get("teams") else m["match_id"]} for m in matches]
    ids = [m["match_id"] for m in matches]
    return options, current if current in ids else (ids[0] if ids else None)


def refresh(match_id: str | None, selected: str | None, last_sig: str | None, rev: int | None = None, store=None):
    """(rundown options, selected card, detail, figure, scoreline, approve/edit/reject disabled, signature); no_update everywhere when nothing changed."""
    store = store or get_store()
    if not match_id:
        return [], None, detail(None, []), figure(None, []), "No match on the desk yet", True, True, True, None
    cards, state, audit = store.cards(match_id), store.state(match_id), store.audit(match_id)
    ids = [c["id"] for c in cards]
    picked = selected if selected in ids else next((c["id"] for c in cards if c.get("status") == "pending"), ids[0] if ids else None)
    sig = hashlib.sha1(json.dumps([cards, (state or {}).get("last_seq"), (state or {}).get("minute_label"), len(audit), picked, match_id, rev], sort_keys=True, default=str).encode()).hexdigest()
    if sig == last_sig:
        return (no_update,) * 9
    card = next((c for c in cards if c["id"] == picked), None)
    # the selection is written back only when it changes, so a refresh never closes an edit in progress
    return (rundown_options(cards), picked if picked != selected else no_update, detail(card, audit), figure(state, cards, picked), scoreline(state), *button_states(card), sig)


def edit_panel(open_: bool, match_id: str | None, card_id: str | None, store=None) -> tuple[list, dict]:
    """The edit fields for every language of the card, and the style of the "Approve edited" button."""
    hidden = {"display": "none"}
    if not open_ or not match_id or not card_id:
        return [], hidden
    card = (store or get_store()).card(match_id, card_id)
    if card is None or not card.get("texts") or card.get("status") == "retracted":
        return [], hidden
    fields = []
    for t in card["texts"]:
        fields.append(html.Div([
            html.Label(LANG_NAMES.get(t["lang"], t["lang"]) + (" (machine-translated)" if t["lang"] in (card.get("machine_translated") or []) else ""), className="lang"),
            dcc.Input(id={"type": "c-edit-headline", "lang": t["lang"]}, value=t.get("headline", ""), type="text", className="edit-headline", debounce=False),
            dcc.Textarea(id={"type": "c-edit-why", "lang": t["lang"]}, value=t.get("why", ""), className="edit-why")], className="edit-block"))
    return fields, {}


def edited_texts(card: dict, edits: dict[str, dict]) -> list[dict] | None:
    """The card's texts with the producer's changes, or None when nothing changed. Claims are kept: a new number must still match a field of the facts."""
    texts, changed = [], False
    for t in card.get("texts") or []:
        e = edits.get(t["lang"], {})
        headline, why = (e.get("headline") if e.get("headline") is not None else t["headline"]).strip(), (e.get("why") if e.get("why") is not None else t["why"]).strip()
        changed |= headline != t["headline"] or why != t["why"]
        texts.append(dict(t, headline=headline, why=why))
    return texts if changed else None


def check_edits(card: dict, texts: list[dict], teams: dict[str, str]) -> list[str]:
    """What the Verifier refuses in edited texts: one line per language."""
    from worker.writer import card_facts, verify

    facts, _ = card_facts(card, teams)
    _, report = verify(json.dumps(texts, ensure_ascii=False), [t["lang"] for t in texts], facts)
    problems = []
    for entry in report["cards"]:
        if entry["verified"]:
            continue
        why = []
        if entry["unclaimed_numbers"]:
            why.append("numbers no fact backs: " + ", ".join(f"{n:g}" for n in entry["unclaimed_numbers"]))
        if entry["wrong_unit"]:
            why.append("not a percentage field: " + ", ".join(f"{n:g}" for n in entry["wrong_unit"]))
        if not entry["keys_ok"]:
            why.append("empty headline or text")
        problems.append(f"{LANG_NAMES.get(entry['lang'], entry['lang'])}: " + ("; ".join(why) or "not verified"))
    return problems


def decide(action: str, match_id: str | None, card_id: str | None, by: str | None, edits: dict[str, dict] | None = None, store=None) -> tuple[str, bool]:
    """Applies a producer decision through the Desk; returns (message, done)."""
    if by is None:
        return "Sign in to approve or reject cards.", False
    if not match_id or not card_id:
        return "Select a card first.", False
    store = store or get_store()
    card = store.card(match_id, card_id)
    if card is None:
        return "This card is no longer on the desk.", False
    try:
        if action == "approve":
            store.approve(match_id, card_id, by)
            return f"On air, approved by {by}.", True
        if action == "reject":
            store.reject(match_id, card_id, by)
            return f"Rejected by {by}.", True
        if action == "approve-edited":
            texts = edited_texts(card, edits or {})
            if texts is None:
                store.approve(match_id, card_id, by)
                return f"No change: on air as written, approved by {by}.", True
            teams = (store.state(match_id) or {}).get("teams") or {card["team"]: card["team_name"], ("away" if card["team"] == "home" else "home"): ""}
            problems = check_edits(card, texts, teams)
            if problems:
                return "Edit refused by the Verifier. " + " | ".join(problems), False
            store.approve(match_id, card_id, by, texts=texts)
            return f"Edited and on air, approved by {by}.", True
    except ValueError as error:
        return f"Refused: {error}.", False
    return f"Unknown action {action}.", False


# --- layout and wiring ----------------------------------------------------------------------------

def layout(user: str) -> html.Div:
    return html.Div([
        dcc.Interval(id="c-tick", interval=1500), dcc.Store(id="c-sig"), dcc.Store(id="c-rev", data=0), dcc.Store(id="c-editing", data=False),
        html.Header([html.Div([html.Span("Watershed", className="brand"), html.Span("producer desk", className="muted")]),
                     dcc.Dropdown(id="c-match", clearable=False, className="match-picker", placeholder="Match"),
                     html.Div(id="c-score", className="scoreline"),
                     dcc.RadioItems(id="c-view", options=panels.VIEWS, value="desk", className="view-switch", inputClassName="view-radio", labelClassName="view-label"),
                     html.Div(f"Signed in as {user}", className="who muted")], className="top"),
        panels.graphics_pane(),
        panels.insights_pane(),
        html.Main([
            html.Section([html.H3("Rundown"), dcc.RadioItems(id="c-rundown", options=[], className="rundown", inputClassName="ru-radio", labelClassName="ru-label")], className="col col-left"),
            html.Section([
                html.Div(id="c-detail"),
                html.Div([html.Button("Approve", id="c-approve", className="btn btn-approve", disabled=True),
                          html.Button("Edit", id="c-edit", className="btn", disabled=True),
                          html.Button("Reject", id="c-reject", className="btn btn-reject", disabled=True)], className="actions"),
                html.Div(id="c-edit-panel", className="edit-panel"),
                html.Div([html.Button("Approve edited", id="c-approve-edit", className="btn btn-approve"), html.Button("Cancel", id="c-cancel-edit", className="btn")],
                         id="c-edit-actions", className="actions", style={"display": "none"}),
                html.Div(id="c-message", className="message", role="status")], className="col col-middle"),
            html.Section([html.H3("Win probability"), dcc.Graph(id="c-wp", config={"displayModeBar": False}),
                          html.P("Filled marker: on air. Faded: waiting or rejected. Cross: retracted.", className="muted small")], className="col col-right"),
        ], id="c-pane-desk", className="desk"),
    ], className="console")


def sign_in() -> html.Div:
    action = (html.A("Sign in", href="/.auth/login/aad?post_login_redirect_uri=/", className="btn btn-approve") if security.easy_auth()
              else html.P("Producer sign-in is not configured on this deployment.", className="muted"))
    return html.Div([html.H1("Watershed producer desk"), html.P("The producer console needs a signed-in producer."), action,
                     html.P(["The public overlay is at ", html.A("/overlay", href="/overlay"), "."], className="muted")], className="sign-in")


def _refresh_cb(_n, rev, match_id, selected, last_sig):
    if security.producer() is None:
        return [], None, html.Div("Sign in to see the desk."), figure(None, []), "", True, True, True, None
    return refresh(match_id, selected, last_sig, rev)


def _edit_cb(_edit, _cancel, _rev, card_id, match_id):
    if security.producer() is None:
        return [], {"display": "none"}
    return edit_panel(ctx.triggered_id == "c-edit", match_id, card_id)


def _decide_cb(_a, _r, _e, match_id, card_id, rev, h_ids, headlines, w_ids, whys):
    action = {"c-approve": "approve", "c-reject": "reject", "c-approve-edit": "approve-edited"}.get(ctx.triggered_id)
    if action is None:
        return no_update, no_update
    edits: dict[str, dict] = {}
    for i, v in zip(h_ids, headlines):
        edits.setdefault(i["lang"], {})["headline"] = v
    for i, v in zip(w_ids, whys):
        edits.setdefault(i["lang"], {})["why"] = v
    message, done = decide(action, match_id, card_id, security.producer(), edits)
    return html.Span(message, className="ok" if done else "error"), (rev or 0) + 1 if done else no_update


def register(app) -> None:
    app.callback(Output("c-match", "options"), Output("c-match", "value"), Input("c-tick", "n_intervals"), State("c-match", "value"))(
        lambda _n, current: match_options(current) if security.producer() is not None else ([], None))
    app.callback(
        Output("c-rundown", "options"), Output("c-rundown", "value"), Output("c-detail", "children"), Output("c-wp", "figure"), Output("c-score", "children"),
        Output("c-approve", "disabled"), Output("c-edit", "disabled"), Output("c-reject", "disabled"), Output("c-sig", "data"),
        Input("c-tick", "n_intervals"), Input("c-rev", "data"), Input("c-match", "value"), Input("c-rundown", "value"), State("c-sig", "data"))(_refresh_cb)
    app.callback(Output("c-edit-panel", "children"), Output("c-edit-actions", "style"),
                 Input("c-edit", "n_clicks"), Input("c-cancel-edit", "n_clicks"), Input("c-rev", "data"), Input("c-rundown", "value"), State("c-match", "value"),
                 prevent_initial_call=True)(_edit_cb)
    app.callback(Output("c-message", "children"), Output("c-rev", "data"),
                 Input("c-approve", "n_clicks"), Input("c-reject", "n_clicks"), Input("c-approve-edit", "n_clicks"),
                 State("c-match", "value"), State("c-rundown", "value"), State("c-rev", "data"),
                 State({"type": "c-edit-headline", "lang": ALL}, "id"), State({"type": "c-edit-headline", "lang": ALL}, "value"),
                 State({"type": "c-edit-why", "lang": ALL}, "id"), State({"type": "c-edit-why", "lang": ALL}, "value"),
                 prevent_initial_call=True)(_decide_cb)
    panels.register(app)
