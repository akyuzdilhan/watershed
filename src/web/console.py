"""The console (`/`): the rundown of cards, the selected card with its texts, the claim behind every number and the Verifier's report, its history, the producer's decisions, and the win-probability curve.

The rundown is in match order, the cards waiting for a decision grouped at the top. The selected card shows its heading, then Approve, Edit and Reject, then its texts in one language at a time (English by default, French and Spanish on the switch next to the buttons). Fact types, fields and audit actions read as words ("Half-time lead lost"), not keys. A card ruled out shows why VAR ruled it out and what the duo says about it (`duo_ruling`). The writing trace (`verification.attempts_log`, `fallback_reason`, `writer_config`) is shown when the card carries it.

Every decision goes through `worker.desk.Desk` (via the store), which writes the audit trail; an edited text is checked by the Verifier before it goes on air. Next to each headline, why and suggested line (cards here, insights and storyline in `panels`), a thumbs up or down, and on a thumbs down one-click reasons and a note: `Desk.feedback`, a decision like approve (refused without a producer), never in the way of approving. The suggested lines are the duo's proposals for the commentary team, never voiced.

In public mode a visitor who is not signed in gets the same views read only: no decision button, no thumbs, reasons or notes, no producer identity, no offside margin. Every decision and feedback callback asks `security.producer()` again and refuses without writing. The functions below are plain functions that `register` wires as callbacks, so the tests call them directly.
"""

import hashlib
import json
import re

import plotly.graph_objects as go
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update

from web import panels, security
from web.overlay import AI_WRITTEN_LABEL, previous_scorer, retraction_message, text_for, var_reason
from web.store import get_store
from worker import feedback as FB
from worker.duo import PERSONAS
from worker.writer import card_facts, claim_ok, offside_call, verify, verify_duo

LANG_NAMES = {"en": "English", "fr": "French", "es": "Spanish"}
LANG_OPTIONS = [{"value": code, "label": name} for code, name in LANG_NAMES.items()]
DEFAULT_LANG = "en"
AI_WRITTEN = AI_WRITTEN_LABEL["en"]  # a language the model and the templates write directly, with no native review
MOMENTS = {"goal": "Goal", "save": "Save", "red_card": "Red card", "chance": "Chance"}
STATUS = {"proposed": ("Writing", "st-proposed"), "pending": ("Pending", "st-pending"), "approved": ("On air", "st-approved"),
          "rejected": ("Rejected", "st-rejected"), "retracted": ("Retracted", "st-retracted")}
VAR_REASONS = {"offside": "Offside", "handball": "Handball", "foul": "Foul"}
VAR_CALLS = {"tight": "tight call", "clear": "clear call"}
ACTIONS = {"written": "Written", "approved": "Approved", "rejected": "Rejected", "edited": "Edited", "retracted": "Retracted", "rewrite_requested": "Rewrite requested",
           "hold": "Held for a VAR check", "release": "Released after the check", "feedback": "Feedback", "superseded": "Superseded"}
FACT_TYPES = {  # the prefix of an insight key (worker.insights, worker.tactics), longest first when matched
    "hat-trick-speed": "Fastest hat-trick", "ht-lead-lost": "Half-time lead lost", "stat-of-the-match": "Stat of the match", "possession-swing": "Possession swing",
    "goals-total": "Goals in the match", "club-goals": "Club goals record", "h2h-drought": "Head-to-head drought", "h2h-margin": "Head-to-head margin",
    "run-unbeaten": "Unbeaten run", "run-winless": "Winless run", "line-break": "Line-breaking pass", "line-height": "Defensive line height",
    "xg-ft": "Full-time xG", "xg-p1": "First-half xG", "shots-ft": "Full-time shots", "shots-p1": "First-half shots", "ten-men": "Ten men",
    "comeback": "Comeback", "scorer": "Scorer milestone", "late": "Late goal", "run": "Run behind the line", "space": "Space before the goal", "pressing": "Pressing",
    "xg": "Expected goals (xG)", "shots": "Shots"}
FACT_KINDS = {"milestone": "Milestone", "rarity": "Rarity", "streak": "Streak", "record": "Record", "tactical": "Tactical detail"}
WORDS = {"wp": "win probability", "pct": "(%)", "pts": "(points)", "xg": "xG", "ht": "half-time", "ft": "full-time", "h2h": "head-to-head", "var": "VAR",
         "m": "(m)", "cm": "(cm)", "s": "(s)", "p1": "first half", "p2": "second half"}
SYSTEM_ACTORS = ("template", "auto-approve", "feed")  # who decided, besides a producer and the writer models
COLORS = {"home": "#3987e5", "draw": "#9a998f", "away": "#d95926", "muted": "#6f6e69", "ink": "#f0efec", "ink2": "#c3c2b7", "surface": "#1a1a19", "grid": "#2c2c2a"}


def _sig(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


# --- readable labels ------------------------------------------------------------------------------

def readable(key: str | None) -> str:
    """A key as words: "rovers_wp_before_pct" reads "Rovers win probability before (%)", "rewrite_requested" "Rewrite requested"."""
    words = [WORDS.get(w.lower(), w) for w in re.split(r"[-_\s]+", str(key or "").strip()) if w]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def fact_label(key: str | None) -> str:
    """What kind of fact an insight is, from its key or group ("ht-lead-lost-away-2" reads "Half-time lead lost") or its kind ("streak" reads "Streak")."""
    k = str(key or "").lower()
    for prefix in sorted(FACT_TYPES, key=len, reverse=True):
        if k == prefix or k.startswith(prefix + "-"):
            return FACT_TYPES[prefix]
    if k in FACT_KINDS:
        return FACT_KINDS[k]
    return readable(re.sub(r"(-(home|away|\d+))+$", "", k))


def field_label(field: str | None) -> str:
    """A fact field as words, for the claims table."""
    return readable(field)


def action_label(action: str | None) -> str:
    return ACTIONS.get(action or "", readable(action))


# --- pieces ---------------------------------------------------------------------------------------

def badges(card: dict) -> list:
    label, cls = STATUS.get(card.get("status"), (card.get("status", "?"), "st-proposed"))
    if card.get("status") == "retracted" and card.get("retracted_on_air"):
        label = "Retracted on air"
    out = [html.Span(label, className=f"badge {cls}")]
    if card.get("under_review"):
        out.append(html.Span("VAR check", className="badge st-var"))
    return out


def rundown_order(cards: list[dict]) -> list[dict]:
    """The rundown: the cards waiting for a decision first, then the others, each group in match order."""
    return sorted(cards, key=lambda c: (c.get("status") != "pending", c.get("clock_s") or 0, c.get("version") or 1, c.get("id", "")))


def rundown_options(cards: list[dict]) -> list[dict]:
    """The rundown entries, in the order given (`rundown_order`)."""
    return [{"value": c["id"], "label": html.Div([
        html.Div([html.Span(c.get("minute_label", ""), className="ru-minute"), html.Span(MOMENTS.get(c.get("moment"), c.get("moment")), className="ru-moment"),
                  html.Span(f"priority {c.get('value_pts', 0)}", className="ru-value")], className="ru-top"),
        html.Div(f"{c.get('team_name', '')}: {c.get('player', {}).get('name', '')}" + (f" (v{c['version']})" if c.get("version", 1) > 1 else ""), className="ru-who"),
        html.Div(badges(c), className="ru-badges")],
        className="ru-item " + {"retracted": "ru-retracted", "pending": "ru-waiting"}.get(c.get("status"), ""))} for c in cards]


def facts_of(card: dict, teams: dict | None) -> tuple[dict, dict]:
    """The facts the card's texts were written from (`worker.writer.card_facts`) and what each field means; empty for a card that cannot be read."""
    if not (teams and "home" in teams and "away" in teams):
        teams = {card.get("team", "home"): card.get("team_name", ""), ("away" if card.get("team") == "home" else "home"): ""}
    try:
        return card_facts(card, teams)
    except (KeyError, TypeError, ValueError, AttributeError):
        return {}, {}


def _num(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


def speaker_name(code: str | None) -> str:
    p = PERSONAS.get(code or "")
    return p.short if p else (code or "?")


def claims_view(t: dict, duo_lines: list[dict], facts: dict, meanings: dict) -> html.Div:
    """Every number of a language with the field it was taken from and what that field means: the text's claims, then those of the suggested lines."""
    rows = []
    for where, claims in [("text", t.get("claims") or [])] + [(speaker_name(d.get("speaker")), d.get("claims") or []) for d in duo_lines]:
        for c in claims if isinstance(claims, list) else []:
            if not isinstance(c, dict):
                continue
            field = str(c.get("source_field", ""))
            ok = claim_ok(c, facts)
            rows.append(html.Tr([html.Td(_num(c.get("value")), className="claim-value"), html.Td(field_label(field), className="fact-key", title=field),
                                 html.Td(meanings.get(field) or ("" if ok else "not a fact of this card"), className="muted small" if ok else "claim-bad small"),
                                 html.Td(where, className="muted small")]))
    if not rows:
        return html.Div("No number in this language.", className="muted small")
    return html.Table([html.Tbody(rows)], className="claims")


def _issue(x) -> str:
    if isinstance(x, dict):
        if "rule" in x:
            return f"{x['rule']}: {x.get('text', '')}"
        return f"{_num(x.get('value'))} ({x.get('source_field', '?')})"
    return _num(x)


TEXT_CHECKS = (("unclaimed_numbers", "every number claimed"), ("bad_claims", "claims match the facts"), ("wrong_unit", "units"), ("style_errors", "style"))


def checks_view(lang: str, t: dict, duo_lines: list[dict], facts: dict, style: bool) -> html.Div:
    """The Verifier's checks on one language as it stands (`worker.writer.verify` and `verify_duo`, no model): what passes, what fails, the warnings."""
    _, report = verify(json.dumps([t], ensure_ascii=False), [lang], facts, style=style)
    entry = (report.get("cards") or [{}])[0]
    pills, ok = [], bool(entry.get("verified"))
    for key, label in TEXT_CHECKS:
        bad = entry.get(key) or []
        pills.append(html.Span(label + (": " + ", ".join(_issue(x) for x in bad) if bad else ""), className="check " + ("check-fail" if bad else "check-ok")))
    if not entry.get("keys_ok", True):
        pills.append(html.Span("headline and text present", className="check check-fail"))
    warnings = [_issue(w) for w in entry.get("style_warnings") or []]
    if entry.get("unknown_names"):
        warnings.append("names not in the facts: " + ", ".join(entry["unknown_names"]))
    if duo_lines:
        d = verify_duo(duo_lines, [lang], facts)
        shape = d["languages"].get(lang, {})
        shape_ok = shape.get("count_ok") and shape.get("alternates") and shape.get("speakers_ok")
        pills.append(html.Span(f"suggested lines: {shape.get('count', 0)}, alternating speakers" if shape_ok else f"suggested lines: {shape.get('count', 0)}, 2 to 4 alternating",
                               className="check " + ("check-ok" if shape_ok else "check-fail")))
        for line in d["lines"]:
            problems = [_issue(x) for k in ("unclaimed_numbers", "bad_claims", "wrong_unit", "style_errors") for x in line.get(k) or []]
            if problems or not line.get("keys_ok", True):
                pills.append(html.Span(f"{speaker_name(line.get('speaker'))}: " + ("; ".join(problems) or "empty line"), className="check check-fail"))
            warnings += [f"{speaker_name(line.get('speaker'))}: {_issue(w)}" for w in line.get("style_warnings") or []]
        ok = ok and bool(d["verified"])
    pills += [html.Span(w, className="check check-warn") for w in warnings]
    verdict = html.Span("Verifier: passes" if ok else "Verifier: fails", className="badge " + ("st-approved" if ok else "st-retracted"))
    return html.Div([verdict, *pills], className="checks")


def language_block(card: dict, t: dict, verdicts: dict | None, facts: dict, meanings: dict, style: bool) -> html.Div:
    lang = t.get("lang", "")
    mt = set(card.get("machine_translated") or [])
    duo_lines = [d for d in card.get("duo") or [] if isinstance(d, dict) and d.get("lang") == lang]
    return html.Div([html.Div([html.Span(LANG_NAMES.get(lang, lang), className="lang"), *([html.Span(AI_WRITTEN, className="badge st-mt")] if lang in mt else [])],
                              className="lang-row"),
                     *panels.text_lines(card, t, verdicts, "d"),
                     claims_view(t, duo_lines, facts, meanings),
                     checks_view(lang, t, duo_lines, facts, style)], className="text-block")


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def var_call_and_margin(card: dict) -> tuple[str | None, float | None]:
    """(tight or clear, margin in metres) of a VAR decision: `var_decision`, else the desk's `ruled_out` (margin in centimetres)."""
    v = card.get("var_decision") if isinstance(card.get("var_decision"), dict) else {}
    call, margin = v.get("call"), v.get("margin_m") if _is_num(v.get("margin_m")) else None
    cm = (card.get("ruled_out") or {}).get("margin_cm") if isinstance(card.get("ruled_out"), dict) else None
    if _is_num(cm):
        margin = cm / 100 if margin is None else margin
        call = offside_call(round(cm)) if call is None and var_reason(card) == "offside" else call
    return call, margin


def corrections(card: dict, show_margin: bool) -> list:
    """What the feed changed on the card: another scorer credited, or VAR ruling it out (the offside margin only for a producer, never on air), and what the lower-third said then."""
    out = []
    prev = previous_scorer(card)
    if prev:
        out.append(html.Div(f"Scorer corrected: {prev} → {card.get('player', {}).get('name', '')}", className="correction"))
    reason = var_reason(card)
    if reason is not None:
        words = VAR_REASONS.get(reason) or (readable(reason) or "Other")
        call, margin = var_call_and_margin(card)
        call = VAR_CALLS.get(call)
        parts = [html.Span(f"VAR decision: {words}" + (f", {call}" if call else ""))]
        if show_margin and margin is not None:
            parts.append(html.Span(f" (margin {margin:.2f} m, producer only: never on air or to visitors)", className="muted small"))
        out.append(html.Div(parts, className="correction"))
    if card.get("status") == "retracted" and card.get("retracted_on_air"):
        out.append(html.Div(f"The lower-third said: {retraction_message(card, 'en')}", className="correction muted"))
    return out


def actor(by: str, action: str, card: dict, read_only: bool) -> str:
    """Who did it, as shown: in the read-only view a producer's identity becomes "a producer"."""
    if not read_only or action == "written" or by in SYSTEM_ACTORS or by in (card.get("writer"), card.get("duo_writer")):
        return by
    return "a producer"


def history_view(card: dict, audit: list[dict], read_only: bool) -> html.Ol:
    entries = [e for e in audit if e.get("card_id") == card["id"] and e.get("by") and not (read_only and e.get("action") == "feedback")]  # desk entries; the Spotter's own lines repeat them
    items = [html.Li([html.Span(action_label(e.get("action")), className="h-action"), html.Span(f" by {actor(e['by'], e.get('action', ''), card, read_only)}"),
                      html.Span(f", v{e['version']}" if e.get("version") else ""), html.Span(f", {readable(e['cause'])}" if e.get("cause") else "", className="muted")])
             for e in entries]
    items = items or [html.Li(f"{action_label(h['action'])} by {actor(h['by'], h['action'], card, read_only)}, v{h['version']}") for h in card.get("history", [])
                      if not (read_only and h.get("action") == "feedback")]
    return html.Ol(items, className="history")


def _usage(a: dict) -> str:
    u = a.get("usage") if isinstance(a.get("usage"), dict) else {}
    return " / ".join(_num(u[k]) if u.get(k) is not None else "-" for k in ("input_tokens", "output_tokens", "reasoning_tokens"))


def writing_trace(doc: dict, read_only: bool) -> list:
    """The writing trace when the document carries it: one row per model attempt (`verification.attempts_log`), why the templates took over (`fallback_reason`), and the writer settings (`writer_config`, producer only). Nothing when the fields are missing."""
    v = doc.get("verification") if isinstance(doc.get("verification"), dict) else {}
    out = []
    log = [a for a in v.get("attempts_log") or [] if isinstance(a, dict)] if isinstance(v.get("attempts_log"), list) else []
    if log:
        head = html.Tr([html.Th(h) for h in ("#", "writer", "verified", "rejected", "finish", "tokens in / out / reasoning", "latency")] + ([] if read_only else [html.Th("error")]))
        rows = [html.Tr([html.Td(_num(a.get("attempt", i + 1))), html.Td(str(a.get("writer") or "?")), html.Td(_num(a.get("verified")) if a.get("verified") is not None else "-"),
                         html.Td(_num(a.get("rejected")) if a.get("rejected") is not None else "-"), html.Td(str(a.get("finish_reason") or "-")), html.Td(_usage(a)),
                         html.Td(f"{a['latency_s']:.1f} s" if _is_num(a.get("latency_s")) else "-")]
                        + ([] if read_only else [html.Td(str(a.get("error") or ""), className="claim-bad small")]))
                for i, a in enumerate(log)]
        out.append(html.Table([html.Thead(head), html.Tbody(rows)], className="facts attempts"))
    if v.get("fallback_reason"):
        out.append(html.Div(f"Written by the templates: {v['fallback_reason']}", className="muted small"))
    cfg = doc.get("writer_config")
    if not read_only and isinstance(cfg, dict) and cfg:
        parts = [f"{k} {cfg[k]}" for k in ("deployment", "effort", "max_tokens") if cfg.get(k) is not None]
        if cfg.get("prompt_sha"):
            parts.append(f"prompt {str(cfg['prompt_sha'])[:10]}")
        out.append(html.Div("Writer settings: " + ", ".join(parts), className="muted small"))
    return out


def card_head(card: dict | None, read_only: bool = False) -> html.Div:
    """The heading of the selected card: minute, moment, player, its badges, and what the feed changed on it."""
    if card is None:
        return html.Div()
    sb = card.get("score_before", {})
    return html.Div([html.H2(f"{card.get('minute_label', '')} {MOMENTS.get(card.get('moment'), card.get('moment'))}: {card.get('player', {}).get('name', '')}"),
                     html.Div([html.Span(f"{card.get('team_name', '')}, score before {sb.get('home', 0)}-{sb.get('away', 0)}, priority {card.get('value_pts', 0)}, version {card.get('version', 1)}", className="muted"),
                               *badges(card)], className="detail-meta"),
                     *corrections(card, show_margin=not read_only)], className="detail-head")


def shown_texts(card: dict, lang: str | None) -> list[dict]:
    """The texts of the language picked (else English, else the first), or every language when `lang` is None."""
    texts = [t for t in card.get("texts") or [] if isinstance(t, dict)]
    if lang is None:
        return texts
    t = text_for({"texts": texts}, lang)
    return [t] if t else []


def detail(card: dict | None, audit: list[dict], feedback: list[dict] | None = None, teams: dict | None = None, read_only: bool = False,
           lang: str | None = DEFAULT_LANG, head: bool = True) -> html.Div:
    """The selected card; `feedback` (the match's feedback documents) marks the current thumbs and gives the approval rate so far; `teams` (the match state's) name both clubs for the claims and the checks. `read_only` (a visitor in public mode): no thumbs, no producer identity, no margin, no feedback.

    `lang` picks the language shown (None: every language); `head` puts the heading (`card_head`) on top, which the console draws apart, above the decision buttons."""
    if card is None:
        return html.Div("Select a card in the rundown.", className="muted")
    verdicts = None if read_only else FB.by_target(feedback or [])
    facts, meanings = facts_of(card, teams)
    texts = shown_texts(card, lang)
    shown_lang = None if lang is None else (texts[0].get("lang") if texts else lang)
    blocks = [language_block(card, t, verdicts, facts, meanings, style=bool(teams)) for t in texts] or [html.Div("Not written yet.", className="muted")]
    v = card.get("verification") or {}
    reps = v.get("repetition_warnings") or 0
    verification = html.Div(
        [html.Span("Verified" if v.get("verified") else "Not verified", className="badge " + ("st-approved" if v.get("verified") else "st-retracted")),
         html.Span(f" when written: writer {card.get('writer', '?')}, {v.get('attempts', 0)} model attempt(s), "
                   + (f"{reps} repetition warning(s) (an opening, phrase or tic already used in this match)" if reps else "no repetition warning"), className="muted"),
         *writing_trace(card, read_only)]
    ) if v else html.Div("Not checked yet.", className="muted")
    out = [card_head(card, read_only)] if head else []
    ruling = panels.duo_block({"id": card["id"], "duo": card.get("duo_ruling")}, lang=shown_lang) if card.get("duo_ruling") else None
    if ruling is not None:
        out += [html.H3("On the VAR ruling"), html.P("What the duo can say about the goal ruled out. Proposed text, never voiced.", className="muted small"), ruling]
    duo = panels.duo_block(card, lang=shown_lang, verdicts=verdicts, pane="d")
    suggested = [html.H3("Suggested lines"), html.P("Proposed text for the commentary team, never voiced.", className="muted small"), duo] if duo is not None else []
    out += [html.Div(blocks), *suggested, html.H3("Verification"), verification, html.H3("History"), history_view(card, audit, read_only)]
    if not read_only:
        out += [html.H3("Producer feedback"), panels.fb_stats(feedback or [])]
    return html.Div(out)


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
    clock = "FT" if state.get("final") else state.get("minute_label", "")
    return f"{t['home']} {s.get('home', 0)}-{s.get('away', 0)} {t['away']}   {clock}"


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


REFRESH_OUTPUTS = 10


def refresh(match_id: str | None, selected: str | None, last_sig: dict | None, rev: int | None = None, store=None, read_only: bool = False, lang: str | None = DEFAULT_LANG):
    """(rundown options, selected card, detail, heading, figure, scoreline, approve/edit/reject disabled, signature); no_update everywhere when nothing changed.

    The rundown is in match order with the cards waiting for a decision on top (`rundown_order`); without a selection the top card is picked. The detail shows the language `lang` (`LANG_NAMES`). It is sent again, with the heading, only when the selected card, its history, the feedback or the language changed: a note being typed survives the ticks that only move the clock or another card. `read_only` (a visitor in public mode) draws the detail without producer controls or data, every button disabled."""
    store = store or get_store()
    lang = lang if lang in LANG_NAMES else DEFAULT_LANG
    if not match_id:
        return [], None, detail(None, []), card_head(None), figure(None, []), "No match on the desk yet", True, True, True, None
    cards, state, audit, fb_docs = rundown_order(store.cards(match_id)), store.state(match_id), store.audit(match_id), store.feedback_docs(match_id)
    ids = [c["id"] for c in cards]
    picked = selected if selected in ids else (ids[0] if ids else None)
    card = next((c for c in cards if c["id"] == picked), None)
    teams = (state or {}).get("teams")
    judged = [(d.get("id"), d.get("verdict"), d.get("reasons"), d.get("note"), d.get("at")) for d in fb_docs]
    detail_sig = _sig(match_id, picked, card, [e for e in audit if e.get("card_id") == picked], judged, teams, rev, read_only, lang)
    sig = _sig(detail_sig, cards, (state or {}).get("last_seq"), (state or {}).get("minute_label"), (state or {}).get("final"), len(audit))
    last = last_sig if isinstance(last_sig, dict) else {}
    if sig == last.get("all"):
        return (no_update,) * REFRESH_OUTPUTS
    changed = detail_sig != last.get("detail")
    view = detail(card, audit, fb_docs, teams, read_only, lang=lang, head=False) if changed else no_update
    head = card_head(card, read_only) if changed else no_update
    buttons = (True, True, True) if read_only else button_states(card)
    # the selection is written back only when it changes, so a refresh never closes an edit in progress
    return (rundown_options(cards), picked if picked != selected else no_update, view, head, figure(state, cards, picked), scoreline(state), *buttons,
            {"all": sig, "detail": detail_sig})


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
            html.Label(LANG_NAMES.get(t["lang"], t["lang"]) + (f" ({AI_WRITTEN})" if t["lang"] in (card.get("machine_translated") or []) else ""), className="lang"),
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


def parse_key(key: str) -> tuple[str, dict]:
    """(document id, target) of a `worker.feedback.target_key`."""
    doc_id, part, lang, index = str(key).rsplit("|", 3)
    return doc_id, {"part": part, "lang": lang, "index": int(index)}


def give_feedback(match_id: str | None, key: str | None, kind: str, value, by: str | None, store=None) -> tuple[str, bool]:
    """A producer's feedback on one sentence: `kind` `verdict` (value `up` or `down`), `reason` (toggles one reason, a thumbs down) or `note` (a thumbs down with this note). Returns (message, done)."""
    if by is None:
        return "Sign in to give feedback.", False
    if not match_id or not key:
        return "Select a sentence first.", False
    store = store or get_store()
    try:
        doc_id, target = parse_key(key)
    except ValueError:
        return "Unknown sentence.", False
    doc = store.doc(match_id, doc_id)
    if doc is None:
        return "This document is no longer on the desk.", False
    try:
        text = FB.sentence(doc, target)["text"]
    except ValueError as error:
        return f"Refused: {error}.", False
    cur = FB.by_target(store.feedback_docs(match_id)).get(key)
    if cur is not None and cur.get("text") != text:
        cur = None  # a judgement on an earlier wording
    down = cur is not None and cur.get("verdict") == "down"
    reasons, note = (list(cur.get("reasons") or []), cur.get("note")) if down else ([], None)
    if kind == "verdict":
        verdict = value
    elif kind == "reason":
        verdict = "down"
        reasons = [r for r in reasons if r != value] if value in reasons else reasons + [value]
    elif kind == "note":
        verdict, note = "down", value
    else:
        return f"Unknown feedback {kind}.", False
    try:
        store.feedback(match_id, doc_id, target, verdict, reasons, note, by)
    except KeyError:
        return "This document is no longer on the desk.", False
    except ValueError as error:
        return f"Refused: {error}.", False
    what = {"headline": "headline", "why": "text", "duo": "duo line"}[target["part"]]
    if verdict == "up":
        return f"Noted: {what} ({target['lang']}) relevant, by {by}.", True
    detail_ = ", ".join(FB.REASONS[r].lower() for r in reasons)
    return f"Noted: {what} ({target['lang']}) not relevant" + (f" ({detail_})" if detail_ else "") + (", with a note" if note else "") + f", by {by}.", True


# --- layout and wiring ----------------------------------------------------------------------------

def viewer(user: str | None) -> html.Div:
    """Who is looking: the signed-in producer, or a visitor of the read-only view (with the sign-in link when Easy Auth is on)."""
    if user:
        return html.Div(f"Signed in as {user}", className="who muted")
    parts = [html.Span("Read-only view", className="muted")]
    if security.easy_auth():
        parts.append(html.A("Producer sign-in", href=security.LOGIN_URL, className="sign-in-link"))
    return html.Div(parts, className="who read-note")


def layout(user: str | None) -> html.Div:
    """The console; without a user (a visitor in public mode) it is read only. The decision buttons stay in the page, hidden, because the refresh callback sets their state."""
    read_only = user is None
    hidden = {"style": {"display": "none"}} if read_only else {}
    return html.Div([
        dcc.Interval(id="c-tick", interval=1500), dcc.Store(id="c-sig"), dcc.Store(id="c-rev", data=0), dcc.Store(id="c-editing", data=False),
        html.Header([html.Div([html.Span("Watershed", className="brand"), html.Span("producer desk", className="muted")]),
                     dcc.Dropdown(id="c-match", clearable=False, className="match-picker", placeholder="Match"),
                     html.Div(id="c-score", className="scoreline"),
                     dcc.RadioItems(id="c-view", options=panels.VIEWS, value="desk", className="view-switch", inputClassName="view-radio", labelClassName="view-label"),
                     viewer(user)], className="top"),
        panels.graphics_pane(),
        panels.insights_pane(),
        html.Main([
            html.Section([html.H3("Rundown"), dcc.RadioItems(id="c-rundown", options=[], className="rundown", inputClassName="ru-radio", labelClassName="ru-label")], className="col col-left"),
            html.Section([
                html.Div(id="c-head"),
                html.Div([html.Div([html.Button("Approve", id="c-approve", className="btn btn-approve", disabled=True),
                                    html.Button("Edit", id="c-edit", className="btn", disabled=True),
                                    html.Button("Reject", id="c-reject", className="btn btn-reject", disabled=True)], className="actions", **hidden),
                          dcc.RadioItems(id="c-lang", options=LANG_OPTIONS, value=DEFAULT_LANG, className="view-switch lang-switch", inputClassName="view-radio",
                                         labelClassName="view-label")], className="detail-bar"),
                html.Div(id="c-message", className="message", role="status"),
                html.Div(id="c-edit-panel", className="edit-panel"),
                html.Div([html.Button("Approve edited", id="c-approve-edit", className="btn btn-approve"), html.Button("Cancel", id="c-cancel-edit", className="btn")],
                         id="c-edit-actions", className="actions", style={"display": "none"}),
                html.Div(id="c-detail")], className="col col-middle"),
            html.Section([html.H3("Win probability"), dcc.Graph(id="c-wp", config={"displayModeBar": False}),
                          html.P("Filled marker: on air. Faded: waiting or rejected. Cross: retracted.", className="muted small")], className="col col-right"),
        ], id="c-pane-desk", className="desk"),
    ], className="console read-only" if read_only else "console")


def _refresh_cb(_n, rev, match_id, selected, lang, last_sig):
    return refresh(match_id, selected, last_sig, rev, read_only=security.producer() is None, lang=lang)


def _edit_cb(_edit, _cancel, _rev, card_id, match_id):
    if security.producer() is None:
        return [], {"display": "none"}
    return edit_panel(ctx.triggered_id == "c-edit", match_id, card_id)


def _decide_cb(_a, _r, _e, match_id, card_id, rev, h_ids, headlines, w_ids, whys):
    action = {"c-approve": "approve", "c-reject": "reject", "c-approve-edit": "approve-edited"}.get(ctx.triggered_id)
    if action is None:
        return no_update, no_update
    by = security.producer()
    if by is None:  # read only: refused before anything is read or written
        return html.Span("Sign in to approve or reject cards.", className="error"), no_update
    edits: dict[str, dict] = {}
    for i, v in zip(h_ids, headlines):
        edits.setdefault(i["lang"], {})["headline"] = v
    for i, v in zip(w_ids, whys):
        edits.setdefault(i["lang"], {})["why"] = v
    message, done = decide(action, match_id, card_id, by, edits)
    return html.Span(message, className="ok" if done else "error"), (rev or 0) + 1 if done else no_update


def _feedback_cb(_v, _r, _n, match_id, rev):
    """Desk pane: the message only (the next tick redraws the thumbs; bumping c-rev would close an edit in progress). Insights pane: the message and a redraw."""
    trig = ctx.triggered_id
    if not isinstance(trig, dict) or not ctx.triggered:
        return no_update, no_update, no_update
    value = ctx.triggered[0].get("value")
    kind = {"fb": "verdict", "fb-r": "reason", "fb-n": "note"}.get(trig.get("type"))
    if kind in ("verdict", "reason") and not value:
        return no_update, no_update, no_update  # a control drawn, not clicked
    if kind == "verdict":
        value = trig.get("v")
    elif kind == "reason":
        value = trig.get("r")
    elif kind == "note":
        value = (value or "").strip()
        cur = FB.by_target(get_store().feedback_docs(match_id)).get(trig.get("k")) if match_id else None
        if value == ((cur or {}).get("note") or ""):
            return no_update, no_update, no_update  # unchanged, or the field just drawn
    by = security.producer()
    if by is None:  # read only: refused, nothing written
        message, done = "Sign in to give feedback.", False
    else:
        message, done = give_feedback(match_id, trig.get("k"), kind, value, by)
    span = html.Span(message, className="ok" if done else "error")
    if trig.get("p") == "i":
        return no_update, span, (rev or 0) + 1 if done else no_update
    return span, no_update, no_update


def register(app) -> None:
    app.callback(Output("c-match", "options"), Output("c-match", "value"), Input("c-tick", "n_intervals"), State("c-match", "value"))(lambda _n, current: match_options(current))
    app.callback(
        Output("c-rundown", "options"), Output("c-rundown", "value"), Output("c-detail", "children"), Output("c-head", "children"), Output("c-wp", "figure"),
        Output("c-score", "children"), Output("c-approve", "disabled"), Output("c-edit", "disabled"), Output("c-reject", "disabled"), Output("c-sig", "data"),
        Input("c-tick", "n_intervals"), Input("c-rev", "data"), Input("c-match", "value"), Input("c-rundown", "value"), Input("c-lang", "value"),
        State("c-sig", "data"))(_refresh_cb)
    app.callback(Output("c-edit-panel", "children"), Output("c-edit-actions", "style"),
                 Input("c-edit", "n_clicks"), Input("c-cancel-edit", "n_clicks"), Input("c-rev", "data"), Input("c-rundown", "value"), State("c-match", "value"),
                 prevent_initial_call=True)(_edit_cb)
    app.callback(Output("c-message", "children"), Output("c-rev", "data"),
                 Input("c-approve", "n_clicks"), Input("c-reject", "n_clicks"), Input("c-approve-edit", "n_clicks"),
                 State("c-match", "value"), State("c-rundown", "value"), State("c-rev", "data"),
                 State({"type": "c-edit-headline", "lang": ALL}, "id"), State({"type": "c-edit-headline", "lang": ALL}, "value"),
                 State({"type": "c-edit-why", "lang": ALL}, "id"), State({"type": "c-edit-why", "lang": ALL}, "value"),
                 prevent_initial_call=True)(_decide_cb)
    app.callback(Output("c-message", "children", allow_duplicate=True), Output("c-i-message", "children", allow_duplicate=True), Output("c-i-rev", "data", allow_duplicate=True),
                 Input({"type": "fb", "p": ALL, "k": ALL, "v": ALL}, "n_clicks"), Input({"type": "fb-r", "p": ALL, "k": ALL, "r": ALL}, "n_clicks"),
                 Input({"type": "fb-n", "p": ALL, "k": ALL}, "value"), State("c-match", "value"), State("c-i-rev", "data"), prevent_initial_call=True)(_feedback_cb)
    panels.register(app)
