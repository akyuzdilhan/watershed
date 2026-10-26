"""The console's match-graphics tab ("All graphics"), its insights and storyline tab ("All insights and storyline"), and under the desk's curve the graphic and insights of the selected card (`card_context`).

Graphics are drawn from the feed (`Store.feed`) with `web.graphics`; tactical details come from `worker.tactics` (pure functions, no model). Insight and storyline documents are read from the store; approve and reject go through `Store.decide_doc`, which uses the desk's own methods for them when it has some and degrades to a message when it has none. When the desk holds no insight yet, the panel lists the tactical candidates computed from the feed, marked as such, with nothing to approve. Every sentence of a document on the desk carries the producer feedback controls (`fb_controls`, wired in `console`). Under the storyline, "After the match" shows the recap the worker writes at the final whistle (`Store.recap`): read only, nothing to approve. Like `console`, the functions below are plain functions that `register` wires as callbacks.

In public mode a visitor who is not signed in gets every panel read only (`read_only`): no thumbs, Approve and Reject disabled, and nothing that stays with the producer, that is the offside margin (run labels, facts, run figures) and the counts, rates and bits behind a rarity (`PRODUCER_ONLY_FACTS`, `rarity.basis`). They are left out on the server, not hidden in the page, and a decision without a producer is refused there too (`decide_doc`). The producer gets them folded under "Producer details" (`producer_details`, closed by default): run labels, insight titles and run figures never carry the margin, for the producer either, so a screen capture of the producer's view shows no margin unless the producer opens the details. Insight types and fact fields read as words (`web.labels`), and a language the model and the templates write directly is labelled like on the overlay ("AI-written, not reviewed").
"""

import hashlib
import json

import plotly.graph_objects as go
from dash import Input, Output, State, ctx, dcc, html, no_update

from web import graphics as G
from web import pitch, security
from web.labels import FACT_KINDS, fact_label, field_label
from web.overlay import AI_WRITTEN_LABEL
from web.matchfeed import player_names, team_names
from web.store import get_store
from worker import feedback as FB
from worker import tactics as T
from worker.duo import PERSONAS

PANEL_REFRESH_MS = 5000  # per IP: the desk ticks every 1.5 s (two callbacks, about 80 requests a minute), the visible panel adds 12 and an overlay open on the same machine 60 (it ticks every second): about 152 a minute, under a RATE_LIMIT_PER_MIN of 600
# The desk tick sets the match picker again with the same value, and Dash runs every callback that takes it as an input each time: the panels take c-p-match instead,
# a copy that this function, run in the browser, writes only when the match changes, so the tick sends them no request.
SAME_MATCH = "function(value, current) { if (value === undefined) { value = null; } return value === (current === undefined ? null : current) ? window.dash_clientside.no_update : value; }"
THEME = dict(pitch.THEMES["dark"], paper="#1a1a19")
KINDS = [
    {"value": "shots", "label": "Shot map"},
    {"value": "momentum", "label": "Momentum and win probability"},
    {"value": "buildups", "label": "Goal and big-chance build-ups"},
    {"value": "setpieces", "label": "Corners and free kicks"},
    {"value": "penalties", "label": "Penalties"},
    {"value": "freekicks", "label": "Direct free kicks"},
    {"value": "tactics", "label": "Tactical details"},
]
DIRECT_KINDS = {"penalties": {"shot_penalty"}, "freekicks": {"shot_freekick"}, "direct": G.DIRECT}
EMPTY = {"penalties": "No penalty in this match.", "freekicks": "No direct free kick in this match."}
STATUS = {"proposed": "Writing", "pending": "Pending", "approved": "On air", "rejected": "Rejected", "superseded": "Superseded", "candidate": "Candidate"}
STATUS_CLASS = {"proposed": "st-proposed", "pending": "st-pending", "approved": "st-approved", "rejected": "st-rejected", "superseded": "st-retracted", "candidate": "st-proposed"}
NONE_ITEM = "none"  # the picker's only, disabled, option when a kind has no item
PRODUCER_ONLY_FACTS = frozenset({  # never on air, never in the read-only view: the offside margin, and the counts, league-wide rates, ratios and bits behind a rarity
    "margin_m", "frame_margin_m", "one_in", "surprisal_bits", "history_count", "history_opportunities", "history_per_season", "runner_up_surprisal_bits", "times_rarer_than_runner_up"})
PRODUCER_ONLY_NOTE = "Offside margins and the counts, rates and bits behind a rarity are shown to the signed-in producer only."
PRODUCER_DETAILS = "Producer details"
AI_WRITTEN = AI_WRITTEN_LABEL["en"]


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


FB_UP, FB_DOWN = "\U0001F44D", "\U0001F44E"
_FB_BTN = {"fontSize": "12px", "fontWeight": "normal", "padding": "0 6px", "lineHeight": "18px", "background": "transparent", "border": "1px solid #2c2c2a",
           "borderRadius": "10px", "color": "#c3c2b7", "cursor": "pointer"}
_FB_ON = {"up": {"borderColor": "#2f9e5b", "background": "rgba(47,158,91,.2)"}, "down": {"borderColor": "#d64545", "background": "rgba(214,69,69,.2)"}}


def fb_controls(pane: str, doc: dict, part: str, lang: str, index: int, text: str, verdicts: dict | None) -> list:
    """Thumbs up and down next to one sentence, and on a thumbs down the one-click reasons and a short note (Enter saves). `verdicts` (`worker.feedback.by_target`) marks the current judgement when it was on this exact text; None draws nothing (a candidate, not on the desk)."""
    if verdicts is None or not text:
        return []
    key = FB.target_key(doc["id"], part, lang, index)
    cur = verdicts.get(key)
    if cur is not None and cur.get("text") != text:
        cur = None  # the sentence was rewritten since
    verdict = (cur or {}).get("verdict")
    label = {"headline": "headline", "why": "text", "duo": "duo line"}[part]
    buttons = [html.Button(FB_UP if v == "up" else FB_DOWN, id={"type": "fb", "p": pane, "k": key, "v": v}, n_clicks=0, className="fb-btn" + (" fb-on" if verdict == v else ""),
                           title=("Relevant" if v == "up" else "Not relevant") + f": this {label} ({lang})", style=dict(_FB_BTN, **(_FB_ON[v] if verdict == v else {})))
               for v in ("up", "down")]
    out = [html.Span(buttons, className="fb", style={"display": "inline-flex", "gap": "4px", "marginLeft": "8px", "verticalAlign": "middle"})]
    if verdict == "down":
        chosen = set(cur.get("reasons") or [])
        chips = [html.Button(name, id={"type": "fb-r", "p": pane, "k": key, "r": r}, n_clicks=0, className="fb-reason" + (" fb-on" if r in chosen else ""),
                             style=dict(_FB_BTN, **(_FB_ON["down"] if r in chosen else {}))) for r, name in FB.REASONS.items()]
        note = dcc.Input(id={"type": "fb-n", "p": pane, "k": key}, value=cur.get("note") or "", type="text", debounce=True, maxLength=FB.NOTE_MAX,
                         placeholder="Note, Enter to save", className="fb-note", style={"fontSize": "12px", "minWidth": "10em", "flex": "1"})
        out.append(html.Div([*chips, note], className="fb-reasons", style={"display": "flex", "flexWrap": "wrap", "gap": "4px", "margin": "4px 0", "fontSize": "12px", "fontWeight": "normal"}))
    return out


def fb_stats(docs: list[dict]) -> html.Div:
    """The approval rate of the match so far, by writer and language."""
    s = FB.stats(docs)
    if not s["votes"]:
        return html.Div("No producer feedback on this match yet.", className="muted small")
    rows = [html.Tr([html.Td(b["writer"], className="fact-key"), html.Td(b["lang"]), html.Td(f"{round(100 * b['rate'])}% of {b['votes']}")]) for b in s["by"]]
    return html.Div([html.Div(f"Approval so far: {round(100 * s['rate'])}% of {s['votes']} sentence(s) judged", className="muted small"),
                     html.Table([html.Tbody(rows)], className="facts")], className="fb-stats")


def speaker(code: str | None) -> str:
    """How a duo line is signed: its persona's short name, else the speaker as written."""
    p = PERSONAS.get(code or "")
    return p.short if p else (code or "?")


def duo_block(doc: dict, lang: str | None = None, verdicts: dict | None = None, pane: str = "i") -> html.Div | None:
    """The duo's suggested lines of a card or insight (`duo: [{speaker, lang, line}]`), grouped by language, each with its feedback controls when `verdicts` is given."""
    lines = [d for d in doc.get("duo") or [] if isinstance(d, dict) and (lang is None or d.get("lang") == lang)]
    if not lines:
        return None
    by_lang: dict[str, list[dict]] = {}
    for d in lines:
        by_lang.setdefault(d.get("lang", "en"), []).append(d)
    return html.Div([html.Div([html.Div(code.upper(), className="lang"),
                               *[html.P([html.Span(f"{speaker(d.get('speaker'))}: ", className="duo-speaker"), d.get("line", ""),
                                         *fb_controls(pane, doc, "duo", code, i, d.get("line", ""), verdicts)], className="duo-line") for i, d in enumerate(items)]],
                              className="duo-lang") for code, items in by_lang.items()], className="duo")


def suggested_lines(duo: html.Div) -> list:
    """The duo's lines under their heading: text proposed to the commentary team, never voiced."""
    return [html.H3("Suggested lines"), html.P("Proposed text for the commentary team, never voiced.", className="muted small"), duo]


def producer_details(children: list) -> html.Details | None:
    """What only the producer sees (offside margin, writer settings, error causes, the basis of a rarity), folded and closed by default; None when there is nothing."""
    children = [c for c in children if c is not None]
    if not children:
        return None
    return html.Details([html.Summary(PRODUCER_DETAILS), *children], open=False, className="producer-details")


def facts_table(facts: dict, meanings: dict) -> html.Table:
    """One row per fact: the field in words (its key on hover), the value, what the field means."""
    return html.Table([html.Tbody([html.Tr([html.Td(field_label(k), className="fact-key", title=k), html.Td(str(v)), html.Td(meanings.get(k, ""), className="muted small")])
                                   for k, v in facts.items()])], className="facts")


def shown_facts(facts: dict) -> dict:
    """The facts shown to everyone: all but `PRODUCER_ONLY_FACTS`, which a visitor never gets and a producer finds under "Producer details"."""
    return {k: v for k, v in facts.items() if k not in PRODUCER_ONLY_FACTS}


def facts_view(facts: dict, meanings: dict, read_only: bool, basis: str | None = None) -> list:
    """The Facts heading and table; for a producer the producer-only facts and the rarity basis folded under "Producer details", for a visitor the note saying they are left out."""
    shown = shown_facts(facts)
    out = [html.H3("Facts"), facts_table(shown, meanings)]
    if read_only:
        if len(shown) < len(facts):
            out.append(html.P(PRODUCER_ONLY_NOTE, className="muted small"))
        return out
    kept = {k: v for k, v in facts.items() if k not in shown}
    folded = producer_details([facts_table(kept, meanings) if kept else None, html.P(f"Rarity: {basis}", className="muted small") if basis else None])
    return out + ([folded] if folded is not None else [])


def text_lines(doc: dict, t: dict, verdicts: dict | None, pane: str) -> list:
    """The headline and the why of one language, each with its feedback controls."""
    lang = t.get("lang", "")
    return [html.Div([t.get("headline", ""), *fb_controls(pane, doc, "headline", lang, 0, t.get("headline", ""), verdicts)], className="headline"),
            html.Div([t.get("why", ""), *fb_controls(pane, doc, "why", lang, 0, t.get("why", ""), verdicts)], className="why")]


def lang_row(lang: str, machine_translated: set) -> html.Div:
    """The language of a text, with the overlay's label when the model and the templates write it directly with no native review (`machine_translated`)."""
    return html.Div([html.Span(lang.upper(), className="lang"), *([html.Span(AI_WRITTEN, className="badge st-mt")] if lang in machine_translated else [])],
                    className="lang-row")


def texts_block(doc: dict, verdicts: dict | None = None, pane: str = "i") -> list:
    mt = set(doc.get("machine_translated") or [])
    return [html.Div([lang_row(t.get("lang", ""), mt), *text_lines(doc, t, verdicts, pane)], className="text-block") for t in doc.get("texts") or []]


# --- match graphics ---------------------------------------------------------------------------------

def graphic_items(feed: dict, kind: str) -> list[dict]:
    """The items of a kind of graphic; a run is labelled without its offside margin, for everyone (`run_margin_details` folds it for the producer)."""
    names = player_names(feed.get("info"))
    teams = team_names(feed.get("info"))
    who = lambda pid: names.get(pid, pid)  # noqa: E731
    if kind == "shots":
        return [{"value": "all", "label": "Both teams"}, *({"value": s, "label": teams[s]} for s in ("home", "away")),
                {"value": "h1", "label": "First half"}, {"value": "h2", "label": "Second half"}]
    if kind == "momentum":
        return [{"value": f"slice:{m}", "label": f"{m}-minute bars"} for m in G.MOMENTUM_SLICES]
    if kind == "buildups":
        return [{"value": a["event_id"], "label": f"{a.get('minute_label', '')} {teams[a['team']]} {'goal' if T.any_goal(a) else 'chance'}" + (" (ruled out)" if a.get("voided") else "")
                 + f", {G._who(a)}, xG {a['shot']['xg']:.2f}"} for a in G.moments(feed)]
    if kind == "setpieces":
        out = [{"value": f"map:{s}", "label": f"All {teams[s]} deliveries"} for s in ("home", "away")]
        return out + [{"value": f"sp:{a['event_id']}", "label": f"{a.get('minute_label', '')} {teams[a['team']]} {pitch.ACTION_LABELS.get(a['action_type'], a['action_type'])}, {G._who(a)}"}
                      for a in feed["actions"] if a["action_type"] in G.SET_PIECES]
    if kind in DIRECT_KINDS:  # "direct", both together, is kept for callers that still ask for it
        return [{"value": a["event_id"], "label": f"{a.get('minute_label', '')} {teams[a['team']]} {'penalty' if a['action_type'] == 'shot_penalty' else 'free kick'}, {G._who(a)}"
                 + (f", xG {a['shot']['xg']:.2f}, {G._lower(G.OUTCOMES[G._outcome(a)][0])}" if a.get("shot") else "")}
                for a in feed["actions"] if a["action_type"] in DIRECT_KINDS[kind]]
    if kind == "tactics":
        moments = G.moments(feed)
        best: dict[tuple[str, str], tuple[dict, dict]] = {}  # (moment, kind) -> (row, item): one line break, one run and one space per goal or chance
        rest = []
        for r in T.line_breaks(feed["actions"], 4):
            m = lead_to(moments, r)
            item = {"value": f"lb:{r['event_id']}", "label": (f"Line break before the {moment_name(m)}" if m else f"Line break {r['minute_label']}")
                    + f": {who(r['passer_id'])}, {r['bypassed']} defenders bypassed"}
            if m is None:
                rest.append((r, item))
            elif (m["event_id"], "lb") not in best or r["bypassed"] > best[(m["event_id"], "lb")][0]["bypassed"]:
                best[(m["event_id"], "lb")] = (r, item)
        for r in T.runs_behind(feed["actions"]):
            m = lead_to(moments, r)
            if m is None and not r["official"]:
                continue  # a run that led nowhere and that VAR did not check tells nothing
            key = (m["event_id"] if m else r["event_id"], "run")
            if key not in best or r["official"] or (r["seq"] > best[key][0]["seq"] and not best[key][0]["official"]):  # the run VAR checked, else the last one before the shot
                best[key] = (r, {"value": f"run:{r['event_id']}:{r['runner_id']}", "label": (f"Run before the {moment_name(m)}" if m else f"Run {r['minute_label']}")
                                 + f": {who(r['runner_id'])} {'beyond' if r['margin_m'] > 0 else 'inside'} the line"})
        shooters = {m["event_id"]: m for m in moments}
        for r in T.space_at_reception(feed["actions"]):
            m = shooters.get(r["event_id"])  # the shooter's room as the ball reached him; space anywhere else is the generator's open midfield, not a story
            if m is not None:
                best[(m["event_id"], "space")] = (r, {"value": f"space:{r['event_id']}", "label": f"Space before the {moment_name(m)}: {who(r['receiver_id'])} {r['space_m']:.1f} m from the nearest opponent"})
        tied = sorted(best.values(), key=lambda p: p[0]["seq"])
        rest = sorted(rest, key=lambda p: (-p[0]["bypassed"], p[0]["seq"]))[:3]
        return ([i for _, i in tied] + [{"value": "lines", "label": "Defensive line height: how high each team defends"},
                                         {"value": "press", "label": f"Closing down: players within {T.PRESS_RADIUS_M:g} m of the ball"}] + [i for _, i in rest])
    return []


LEAD_S = 60.0  # a tactical detail is tied to a goal or chance that its team reached within this many seconds


def lead_to(moments: list[dict], row: dict) -> dict | None:
    """The goal or chance (`graphics.moments`) of the same team that followed `row` within `LEAD_S` seconds in the same period, if any."""
    return next((m for m in moments if m["team"] == row["team"] and m.get("period") == row.get("period") and 0 <= m["clock_s"] - row["clock_s"] <= LEAD_S), None)


def moment_name(m: dict) -> str:
    """The goal or chance in words: "52' goal", "63' goal ruled out", "72' chance"."""
    return f"{m.get('minute_label', '')} " + ("goal ruled out" if m.get("voided") else "goal" if T.any_goal(m) else "chance")


def graphic_note(feed: dict, kind: str, item: str | None) -> str:
    """One plain line under a graphic: what it measures and why it matters, for the item picked when the kind has several (tactical details); never the offside margin."""
    if kind != "tactics" or not item:
        return NOTES.get(kind, "")
    tag = item.partition(":")[0]
    if tag == "lines":
        return ("How high each team defends: the distance from its own goal to its deepest outfield player while the opponent has the ball. A high line squeezes the space; "
                "shaded: the ten minutes after a goal when the team that conceded dropped deeper.")
    if tag == "press":
        return (f"How many players a team gets within {T.PRESS_RADIUS_M:g} m of the opponent on the ball, in open play. More players close means harder pressing "
                "and less time on the ball.")
    if tag == "lb":
        return "A pass that went past the ringed defenders: each one bypassed is one fewer between the ball and the goal. Shaded: the stretch of pitch the pass crossed."
    if tag == "run":
        return ("A pass played behind the defensive line (dashed: the second-last defender, keeper included). Where the runner stood against that line when the ball was played "
                "decides offside.")
    if tag == "space":
        return ("How much room the receiver had when the ball arrived: the circle reaches the nearest opponent. With a few metres a player can turn and choose; "
                "under two he has to play first time.")
    return NOTES["tactics"]


def _tactic_figure(feed: dict, tag: str, rest: str) -> go.Figure:
    if tag == "lb":
        return _sized(G.line_break_figure(feed, rest, THEME, None))
    if tag == "run":
        event_id, _, runner = rest.partition(":")
        return _sized(G.run_figure(feed, event_id, runner or None, THEME, width=None, show_margin=False))
    return _sized(G.space_figure(feed, rest, THEME, None))


def _tied(feed: dict, fig: go.Figure, event_id: str) -> go.Figure:
    """The title of a tactical detail says which goal or chance it led to, when it led to one within `LEAD_S`."""
    a = next((x for x in feed["actions"] if x["event_id"] == event_id), None)
    m = lead_to(G.moments(feed), a) if a else None
    if m is not None and fig.layout.title.text:
        fig.layout.title.text = fig.layout.title.text.replace("</b>", f", before the {moment_name(m)}</b>", 1)
    return fig


COMPACT_BUILDUP = 8  # actions of a build-up drawn under the desk's curve; All graphics draws the last 10


def graphic_figure(feed: dict, kind: str, item: str | None, timeline: list[dict] | None = None, compact: bool = False) -> go.Figure:
    """The figure of a graphic; a run is drawn without its offside margin, for everyone. `compact` (under the desk's curve): a build-up keeps its last `COMPACT_BUILDUP` actions."""
    if kind == "shots":
        team = item if item in ("home", "away") else None
        half = {"h1": 1, "h2": 2}.get(item or "")
        return _sized(G.shot_map(feed, THEME, None, team=team, half=half), 620)
    if kind == "momentum":
        minutes = int(item.partition(":")[2]) if item and item.startswith("slice:") and item.partition(":")[2].isdigit() else 5
        return _sized(G.momentum_figure(feed, timeline, THEME, minutes=minutes, width=None), 520)
    if not item:
        return empty_figure(EMPTY.get(kind, "Nothing of this kind in the match so far."))
    if kind == "buildups":
        return _sized(G.buildup_figure(feed, item, THEME, None, **({"n_max": COMPACT_BUILDUP} if compact else {})))
    if kind == "setpieces":
        tag, _, rest = item.partition(":")
        return _sized(G.set_pieces_map(feed, rest, THEME, None) if tag == "map" else G.set_piece_figure(feed, rest, THEME, None), 640)
    if kind in DIRECT_KINDS:
        if next(a for a in feed["actions"] if a["event_id"] == item)["action_type"] == "shot_freekick":
            return _sized(G.freekick_figure(feed, item, THEME, None), 480)
        return _sized(G.shot_on_goal_figure(feed, item, THEME, None), 360)
    if kind == "tactics":
        tag, _, rest = item.partition(":")
        if tag in ("lb", "run", "space"):
            return _tied(feed, _tactic_figure(feed, tag, rest), rest.partition(":")[0])
        if tag == "lines":
            return _sized(G.line_height_figure(feed, THEME, width=None), 440)
        if tag == "press":
            return _sized(G.pressing_figure(feed, THEME, width=None), 420)
    return empty_figure("Pick a graphic.")


NOTES = {
    "shots": "Marker size is the xG of the shot (key under the pitch); pick a team or a half above. Purple crosses are goals VAR ruled out: drawn, but left out of the goals, shots and xG above the pitch.",
    "momentum": "Threat added: value of where completed passes and carries ended minus where they started (value = xG of a shot from there). Pick the bar width above; the thick lines average about 15 minutes. Win probability from the desk's match state.",
    "buildups": "The possession behind the goal or chance (last 10 actions), numbered in order and told in words under the pitch: solid arrows passes, dashed carries, dotted curves crosses, the thick arrow the shot in its outcome's colour. The star is where the ball was won, filled gold for a high regain in the opponent's half. Arrow width: threat added; the shot's: xG.",
    "setpieces": "Delivery width: value of the landing point. One corner or free kick plays in three steps: Play runs them, the slider under the pitch jumps to one (1 Delivery, 2 First contact, 3 What followed).",
    "penalties": "Seen from behind the taker: where the ball crossed the line, and the box where the keeper stood in the frame.",
    "freekicks": "From above: the kick spot and its distance to goal, the wall (defenders within 11 m on the line to goal, from the freeze frame), the keeper and the shot. On the right the goal seen from behind the taker.",
    "direct": "Seen from behind the taker. The box shows where the keeper stood in the frame.",
    "tactics": "Measured on the 360 freeze frames (synthetic positions, rounded to 0.1 m). Details that led to a goal or a chance within a minute come first.",
}


def run_margin_details(feed: dict, item: str | None) -> html.Details | None:
    """The offside margin of the run picked in the tactics graphics, folded under "Producer details"; None for any other item."""
    tag, _, rest = (item or "").partition(":")
    if tag != "run":
        return None
    event_id, _, runner = rest.partition(":")
    run = next((r for r in T.runs_behind(feed["actions"]) if r["event_id"] == event_id and (not runner or r["runner_id"] == runner)), None)
    if run is None:
        return None
    side = "beyond" if run["margin_m"] > 0 else "inside"
    return producer_details([html.Div(f"Offside margin {abs(run['margin_m']):.2f} m {side} the line" + (", as VAR measured it" if run.get("official") else ", measured on the frame")
                                      + ": never on air or to visitors", className="muted small")])


def graphics(match_id: str | None, kind: str | None, item: str | None, view: str | None, last_sig: str | None, store=None, read_only: bool = False):
    """(item options, item value, figure, note, signature); no_update everywhere when the tab is hidden or nothing changed. Labels and figures never carry the offside margin; a producer (not `read_only`) finds the margin of the run picked under "Producer details" in the note."""
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
    options = graphic_items(feed, kind) or [{"value": NONE_ITEM, "label": EMPTY.get(kind, "Nothing of this kind yet").rstrip("."), "disabled": True}]
    values = [o["value"] for o in options if not o.get("disabled")]
    picked = item if item in values else (values[-1] if kind in ("buildups",) and values else values[0] if values else None)
    sig = _sig(match_id, kind, picked, feed["last_seq"], state.get("last_seq") if kind == "momentum" else None, read_only)
    if sig == last_sig:
        return (no_update,) * 5
    try:
        fig = graphic_figure(feed, kind, picked, state.get("wp_timeline") if kind == "momentum" else None)
    except (StopIteration, KeyError, ValueError, IndexError) as error:
        fig = empty_figure(f"Cannot draw this graphic: {error}")
    folded = None if read_only or kind != "tactics" else run_margin_details(feed, picked)
    line = graphic_note(feed, kind, picked)
    note = line if folded is None else [html.P(line), folded]
    return options, picked if picked != item else no_update, fig, note, sig


def graphics_pane() -> html.Section:
    return html.Section([
        html.Div([dcc.Dropdown(id="c-g-kind", options=KINDS, value="shots", clearable=False, className="match-picker"),
                  dcc.Dropdown(id="c-g-item", options=[], clearable=False, className="match-picker g-item", placeholder="Nothing to pick")], className="g-controls"),
        dcc.Graph(id="c-g-figure", figure=empty_figure("Loading."), config={"displayModeBar": False}, className="g-figure"),
        html.Div(id="c-g-note", className="muted small"), dcc.Store(id="c-g-sig"), dcc.Store(id="c-p-match"), dcc.Interval(id="c-g-tick", interval=PANEL_REFRESH_MS, disabled=True)],
        id="c-pane-graphics", className="col pane", style={"display": "none"})


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


def insight_type(d: dict) -> str:
    """What kind of fact an insight is, in words: from its group or key ("Half-time lead lost", "Run behind the line"), else its kind ("Streak")."""
    return fact_label(d.get("group") or d.get("key") or d.get("kind"))


def insight_options(docs: list[dict]) -> list[dict]:
    return [{"value": d["id"], "label": html.Div([
        html.Div([html.Span(d.get("minute_label") or "", className="ru-minute"), html.Span(insight_type(d), className="ru-moment"),
                  html.Span(f"priority {d.get('value', 0)}", className="ru-value")], className="ru-top"),
        html.Div(insight_title(d), className="ru-who"),
        html.Div(html.Span(STATUS.get(d.get("status"), d.get("status", "?")), className="badge " + STATUS_CLASS.get(d.get("status"), "st-proposed")), className="ru-badges")],
        className="ru-item")} for d in docs]


def insight_title(d: dict) -> str:
    """The English headline, else a title made from the facts (a candidate has no text), a run without its margin (the producer finds it under "Producer details"); else the insight's type in words."""
    texts = d.get("texts") or []
    en = next((t for t in texts if t.get("lang") == "en"), texts[0] if texts else None)
    if en:
        return en.get("headline", "")
    f = d.get("headline_facts") or {}
    kind = (d.get("figure") or {}).get("kind")
    if kind == "line_break" and "defenders_bypassed" in f:
        return f"{f.get('passer_name', '')}: {f['defenders_bypassed']} defenders bypassed before the {f.get('goal_minute_label', '')} goal"
    if kind == "run" and "margin_m" in f:
        return f"{f.get('runner_name', '')}: {'beyond' if f.get('runner_beyond_line') else 'inside'} the line"
    if kind == "space" and "space_m" in f:
        return f"{f.get('scorer_name', '')}: {f['space_m']} m of space before the {f.get('goal_minute_label', '')} goal"
    if kind == "line_height":
        return f"{f.get('team_name', '')} defensive line {f.get('line_moved', 'moved')} by {f.get('change_m', '?')} m"
    if kind == "pressing":  # what its figure counts, "Opponents near the ball"
        more = {"harder": "more", "less": "fewer"}.get(f.get("pressed"))
        return f"{f.get('team_name', '')}: " + (f"{more} players near the opponent on the ball after the break" if more else "players near the opponent on the ball, first half against second")
    return insight_type(d) if (d.get("group") or d.get("key") or d.get("kind")) else d.get("id", "")


def insight_detail(d: dict | None, feed: dict | None, from_desk: bool, verdicts: dict | None = None, read_only: bool = False) -> tuple[html.Div, go.Figure]:
    """The selected insight: texts, duo lines, facts and figure. `read_only` (a visitor): no thumbs, no producer-only fact, no rarity basis; a producer gets the producer-only facts (the offside margin among them) and the rarity basis folded (`facts_view`). A run figure never carries its margin."""
    if d is None:
        return html.Div("No insight yet. Tactical candidates appear here once the feed has goals to explain.", className="muted"), empty_figure("No insight selected.")
    rarity = d.get("rarity") or {}
    kind, what = FACT_KINDS.get(str(d.get("kind") or "").lower(), str(d.get("kind") or "")).lower(), insight_type(d)
    parts = [html.H2(insight_title(d)),
             html.Div([html.Span(f"{d.get('minute_label') or ''} {what}" + (f", {kind}" if kind and kind != what.lower() else "") + f", priority {d.get('value', 0)}", className="muted"),
                       html.Span(STATUS.get(d.get("status"), d.get("status", "?")), className="badge " + STATUS_CLASS.get(d.get("status"), "st-proposed"))], className="detail-meta")]
    if not from_desk:
        parts.append(html.P("Candidate computed from the feed by worker.tactics: not on the desk, no text yet, nothing to approve.", className="muted small"))
    fb = (verdicts if verdicts is not None else {}) if from_desk and not read_only else None
    parts += texts_block(d, fb)
    duo = duo_block(d, verdicts=fb)
    if duo is not None:
        parts += suggested_lines(duo)
    basis = (str(rarity["basis"]) + (f" ({rarity['surprisal_bits']} bits)" if rarity.get("surprisal_bits") is not None else "")) if rarity.get("basis") else None
    parts += facts_view(d.get("headline_facts") or {}, d.get("field_meanings") or {}, read_only, basis)
    fig = G.insight_figure(d, feed, THEME, None, show_margin=False) if feed else None
    return html.Div(parts), _sized(fig) if fig is not None else empty_figure("No figure for this insight.")


def storyline_block(doc: dict | None, verdicts: dict | None = None, read_only: bool = False) -> html.Div:
    """The storyline: texts, duo lines and facts; `read_only` (a visitor) draws no thumbs and leaves the producer-only facts out."""
    if not doc:
        return html.Div("No storyline for this match yet.", className="muted")
    parts = [html.Div([html.Span(STATUS.get(doc.get("status"), doc.get("status", "")), className="badge " + STATUS_CLASS.get(doc.get("status"), "st-proposed"))], className="detail-meta")]
    fb = None if read_only else (verdicts if verdicts is not None else {})
    parts += texts_block(doc, fb)
    duo = duo_block(doc, verdicts=fb)
    if duo is not None:
        parts += suggested_lines(duo)
    parts += facts_view(doc.get("facts") or doc.get("headline_facts") or {}, doc.get("field_meanings") or {}, read_only)
    return html.Div(parts)


RECAP_PARTS = {"recap": "Recap", "fan": "Supporters", "analyst": "Analyst"}


def recap_block(doc: dict | None, final: bool, lang: str | None = None) -> html.Div:
    """After the match: the recap, the supporters' text and the analyst text of the recap document (`worker.recap`), in every language or in `lang` only, labelled like the other texts of the console (Spanish "AI-written, not reviewed"). Before the final whistle, a line saying when it comes."""
    if not final:
        return html.Div("The recap appears at full time.", className="muted")
    if not doc:
        return html.Div("Full time: no recap yet.", className="muted")
    teams = {"fan": doc.get("fan_team"), "analyst": doc.get("analyst_team")}
    mt = set(doc.get("machine_translated") or [])
    blocks = []
    for t in doc.get("texts") or []:
        if not isinstance(t, dict) or (lang is not None and t.get("lang") != lang):
            continue
        lines = [html.P([html.Span(label + (f" ({teams[part]})" if teams.get(part) else "") + ": ", className="duo-speaker"), t[part]], className="duo-line recap-line")
                 for part, label in RECAP_PARTS.items() if isinstance(t.get(part), str) and t[part].strip()]
        blocks.append(html.Div([lang_row(t.get("lang", ""), mt), *lines], className="text-block"))
    v = doc.get("verification") or {}
    meta = [html.Span("Verified" if v.get("verified") else "Not verified", className="badge " + ("st-approved" if v.get("verified") else "st-retracted")),
            html.Span(f"writer {doc.get('writer', '?')}" + ("" if doc.get("final", True) else ", written before the final whistle"), className="muted")]
    return html.Div([html.Div(meta, className="detail-meta"), *(blocks or [html.Div("No text in this recap.", className="muted")])])


def same_match(recap: dict, state: dict) -> bool:
    """Whether a recap was written from this match state: the score and the teams of its analysis are the state's (a recap without them is taken as it is)."""
    analysis = recap.get("analysis") or {}
    return all(analysis.get(k) is None or analysis[k] == state.get(k) for k in ("score", "teams"))


def after_the_match(match_id: str | None, view: str | None, last_sig: str | None, store=None, lang: str | None = None):
    """(After the match section, signature); no_update for both when the tab is hidden or nothing changed. The recap is read once the match state is final, so a recap left by an earlier run of the match never shows while it is played again, and at full time one with another score or other teams is not shown (`same_match`)."""
    if view != "insights":
        return no_update, no_update
    store = store or get_store()
    state = (store.state(match_id) or {}) if match_id else {}
    final = bool(state.get("final"))
    doc = store.recap(match_id) if final else None
    if doc is not None and not same_match(doc, state):
        doc = None  # left by an earlier run that ended otherwise, or written before a later goal: the worker's recap replaces it
    sig = _sig(match_id, final, doc, lang)
    if sig == last_sig:
        return no_update, no_update
    return recap_block(doc, final, lang), sig


def insights(match_id: str | None, selected: str | None, view: str | None, last_sig: str | None, rev: int | None = None, store=None, read_only: bool = False):
    """(options, selected, detail, figure, storyline, approve disabled, reject disabled, storyline approve disabled, storyline reject disabled, signature). `read_only` (a visitor in public mode): every button disabled, no thumbs, nothing kept for the producer."""
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
    fb_docs = [] if read_only else store.feedback_docs(match_id)
    sig = _sig(match_id, docs, story, picked, rev, (feed or {}).get("last_seq"), len(fb_docs), read_only)
    if sig == last_sig:
        return (no_update,) * 10
    doc = next((d for d in docs if d["id"] == picked), None)
    verdicts = FB.by_target(fb_docs)
    detail, fig = insight_detail(doc, feed, from_desk, verdicts, read_only)
    status = (doc or {}).get("status")
    no_doc = read_only or not from_desk or doc is None
    no_story = read_only or story is None
    s_status = (story or {}).get("status")
    return (insight_options(docs), picked if picked != selected else no_update, detail, fig, storyline_block(story, verdicts, read_only),
            no_doc or status in ("approved", "superseded"), no_doc or status in ("rejected", "superseded"),
            no_story or s_status == "approved", no_story or s_status == "rejected", sig)


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
    return ("On air" if action == "approve" else "Rejected") + f", by {security.display_name()}.", True


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
                                html.Button("Reject", id="c-s-reject", className="btn btn-reject", disabled=True)], className="actions"),
                      html.H3("After the match"), html.Div(recap_block(None, False), id="c-r-detail")], className="col col-right"),
        dcc.Store(id="c-i-sig"), dcc.Store(id="c-r-sig"), dcc.Store(id="c-i-rev", data=0), dcc.Interval(id="c-i-tick", interval=PANEL_REFRESH_MS, disabled=True)],
        id="c-pane-insights", className="desk pane", style={"display": "none"})


# --- the selected card's graphic and insights, under the desk's curve ----------------------------------

def card_graphic(feed: dict | None, card: dict | None) -> tuple[str, str | None]:
    """(kind, item) of the match graphic that goes with a card: the build-up of its goal, chance or save, a penalty seen from behind the taker; ("", None) for a card with none (a red card)."""
    if not feed or not feed.get("actions") or not card:
        return "", None
    shot = card.get("shot_event_id") or card.get("event_id")
    for kind in ("direct", "buildups"):
        if shot in [o["value"] for o in graphic_items(feed, kind)]:
            return kind, shot
    return "", None


def tied_insights(docs: list[dict], card: dict | None, limit: int = 3) -> list[dict]:
    """The insights about a card's moment: written at it, explaining it (a run or line break before the goal) or counting it, those still standing first."""
    if not card:
        return []
    ev = card.get("event_id")

    def rank(d: dict) -> int | None:
        if d.get("event_id") == ev or (d.get("figure") or {}).get("goal_event_id") == ev:
            return 0
        return 1 if ev in (d.get("evidence") or []) else None

    tied = [(rank(d), d) for d in docs]
    tied = sorted(((r, d) for r, d in tied if r is not None), key=lambda x: (x[1].get("status") == "superseded", x[0], -(x[1].get("value") or 0)))
    return [d for _, d in tied[:limit]]


def text_in(doc: dict | None, lang: str | None) -> dict | None:
    """The text of a document in `lang`, else English, else the first."""
    texts = [t for t in (doc or {}).get("texts") or [] if isinstance(t, dict)]
    return next((t for t in texts if t.get("lang") == lang), next((t for t in texts if t.get("lang") == "en"), texts[0] if texts else None))


def insight_line(d: dict, lang: str | None) -> html.Div:
    """One insight in a few words: its minute and type, its status, then its headline and why in the language picked (a candidate: its title)."""
    t = text_in(d, lang)
    words = [html.Div(t.get("headline", ""), className="ctx-headline"), html.Div(t.get("why", ""), className="why small")] if t else [html.Div(insight_title(d), className="ctx-headline")]
    return html.Div([html.Div([html.Span(f"{d.get('minute_label') or ''} {insight_type(d)}", className="muted small"),
                               html.Span(STATUS.get(d.get("status"), d.get("status", "?")), className="badge " + STATUS_CLASS.get(d.get("status"), "st-proposed"))], className="detail-meta"),
                     *words], className="ctx-insight")


def card_context(card: dict | None, feed: dict | None, docs: list[dict], story: dict | None, lang: str | None = None) -> list:
    """Under the desk's curve: the match graphic of the selected card (`card_graphic`), the insights tied to it (`tied_insights`) and the storyline's headline. Nothing that stays with the producer: no fact table, no margin (a run is drawn without it)."""
    if card is None:
        return []
    out = [html.H3("This moment on the pitch")]
    kind, item = card_graphic(feed, card)
    if kind:
        try:
            fig = _sized(graphic_figure(feed, kind, item, compact=True), 440)
        except (StopIteration, KeyError, ValueError, IndexError) as error:
            fig = empty_figure(f"Cannot draw this graphic: {error}")
        note = NOTES[kind].replace("(last 10 actions)", f"(last {COMPACT_BUILDUP} actions here, 10 under All graphics)") if kind == "buildups" else NOTES[kind]
        out += [dcc.Graph(figure=fig, config={"displayModeBar": False}, className="ctx-figure"), html.P(note, className="muted small")]
    else:
        out.append(html.P("No pitch graphic for this moment; every graphic of the match is under All graphics.", className="muted small"))
    out.append(html.H3("Insights on this moment"))
    out += [insight_line(d, lang) for d in tied_insights(docs, card)] or [html.P("None for this moment; every insight is under All insights and storyline.", className="muted small")]
    t = text_in(story, lang)
    if t:
        out += [html.H3("Storyline"), html.Div(t.get("headline", ""), className="ctx-headline")]
    return out


# --- wiring ------------------------------------------------------------------------------------------

VIEWS = [{"value": "desk", "label": "Desk"}, {"value": "graphics", "label": "All graphics"}, {"value": "insights", "label": "All insights and storyline"}]


def view_styles(view: str | None) -> tuple[dict, dict, dict, bool, bool]:
    """Pane styles, then whether the graphics and insights refresh timers are off: a hidden pane sends no request (the rate limit counts every callback)."""
    show, hide = {}, {"display": "none"}
    view = view or "desk"
    return (show if view == "desk" else hide, show if view == "graphics" else hide, show if view == "insights" else hide,
            view != "graphics", view != "insights")


def _graphics_cb(_n, match_id, kind, item, view, last_sig):
    """Every visitor sees the graphics; only a producer gets the folded offside margin of a run."""
    return graphics(match_id, kind, item, view, last_sig, read_only=security.producer() is None)


def _insights_cb(_n, rev, match_id, selected, view, last_sig, recap_sig):
    """The insights and storyline outputs, then the After the match section, in one request per tick; read only without a producer (public mode)."""
    return insights(match_id, selected, view, last_sig, rev, read_only=security.producer() is None) + after_the_match(match_id, view, recap_sig)


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
    app.clientside_callback(SAME_MATCH, Output("c-p-match", "data"), Input("c-match", "value"), State("c-p-match", "data"))
    app.callback(Output("c-g-item", "options"), Output("c-g-item", "value"), Output("c-g-figure", "figure"), Output("c-g-note", "children"), Output("c-g-sig", "data"),
                 Input("c-g-tick", "n_intervals"), Input("c-p-match", "data"), Input("c-g-kind", "value"), Input("c-g-item", "value"), Input("c-view", "value"),
                 State("c-g-sig", "data"))(_graphics_cb)
    app.callback(Output("c-i-list", "options"), Output("c-i-list", "value"), Output("c-i-detail", "children"), Output("c-i-figure", "figure"), Output("c-s-detail", "children"),
                 Output("c-i-approve", "disabled"), Output("c-i-reject", "disabled"), Output("c-s-approve", "disabled"), Output("c-s-reject", "disabled"), Output("c-i-sig", "data"),
                 Output("c-r-detail", "children"), Output("c-r-sig", "data"),
                 Input("c-i-tick", "n_intervals"), Input("c-i-rev", "data"), Input("c-p-match", "data"), Input("c-i-list", "value"), Input("c-view", "value"),
                 State("c-i-sig", "data"), State("c-r-sig", "data"))(_insights_cb)
    app.callback(Output("c-i-message", "children"), Output("c-i-rev", "data"),
                 Input("c-i-approve", "n_clicks"), Input("c-i-reject", "n_clicks"), Input("c-s-approve", "n_clicks"), Input("c-s-reject", "n_clicks"),
                 State("c-match", "value"), State("c-i-list", "value"), State("c-i-rev", "data"), prevent_initial_call=True)(_decide_cb)
