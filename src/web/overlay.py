"""The public overlay (`/overlay`): a read-only lower-third with the card on air, and a visible retraction when the feed takes back a card that was on air.

A card holds the lower-third for `CARD_HOLD_S` seconds of match clock after it goes up, and while VAR checks it: a fact (insight) approved meanwhile, often in the same feed message as the goal, waits on a detail line under the card and takes the screen once the card's time is up. The retraction of a card that was on air holds the screen the same way, for `RETRACTION_HOLD_S`, and then leaves it: "Goal ruled out (VAR, offside)" with the reason VAR gave, never the margin.

Tags: "auto-approved" when the last approval of what is on air came from the desk's auto-approval (the audit's `by`), "AI-written, not reviewed" on a language the model and the templates write directly with no native review (`machine_translated`). At the final whistle the score bug reads "FT", and an element written at the final whistle shows "FT" and the final score instead of its minute.

Query string: `match` (default: the first match), `lang` (`en`, `fr`, `es`), `clean=1` to hide the language links (for a video mixer). This module reads the store and nothing else: no Writer, no model, no producer decision, and never the VAR margin.
"""

import re
from urllib.parse import parse_qs, urlencode

from dash import Input, Output, dcc, html

from web.store import get_store

LANGS = ("en", "fr", "es")
AI_WRITTEN_LABEL = {"en": "AI-written, not reviewed", "fr": "rédigé par IA, non relu", "es": "escrito por IA, sin revisar"}
AUTO_APPROVED_LABEL = {"en": "auto-approved", "fr": "validation automatique", "es": "aprobación automática"}
AUTO_APPROVER = "auto-approve"  # the audit's `by` for an approval the desk made itself
VAR_LABEL = {"en": "VAR check", "fr": "Vérification VAR", "es": "Revisión VAR"}
SCORER_CORRECTED = {"en": "Scorer corrected", "fr": "Buteur corrigé", "es": "Gol reasignado"}
RULED_OUT = {"en": "Goal ruled out", "fr": "But refusé", "es": "Gol anulado"}
VAR_REASON_WORDS = {
    "offside": {"en": "offside", "fr": "hors-jeu", "es": "fuera de juego"},
    "handball": {"en": "handball", "fr": "main", "es": "mano"},
    "foul": {"en": "foul", "fr": "faute", "es": "falta"},
}
WITHDRAWN = {"en": "Card withdrawn", "fr": "Info retirée", "es": "Información retirada"}
FULL_TIME = "FT"
CARD_HOLD_S = 25.0  # match seconds a card keeps the lower-third after it goes up
RETRACTION_HOLD_S = 20.0  # match seconds a retraction stays on the lower-third before it leaves


def minute_end(label: str | None) -> float | None:
    """Where a minute label ends on the match clock: "64'" before 3840 s, "45+2'" before 2820 s."""
    m = re.match(r"\s*(\d+)(?:\+(\d+))?", label or "")
    return 60.0 * (int(m.group(1)) + int(m.group(2) or 0)) if m else None


def label_period(label: str | None) -> int | None:
    """The period of a minute label: "45+2'" is still the first half, "46'" the second."""
    m = re.match(r"\s*(\d+)(\+)?", label or "")
    if not m:
        return None
    n = int(m.group(1))
    if m.group(2):
        return {45: 1, 90: 2, 105: 3, 120: 4}.get(n)
    return 1 if n <= 45 else 2 if n <= 90 else 3 if n <= 105 else 4


def went_up(doc: dict, entry: dict, written_at: float | None, feed_end: float | None) -> float | None:
    """When a card went up (or came down) on the match clock: its moment, or later when the producer decided later (wall seconds since it was first written) or the feed acted on it since (the end of that minute)."""
    base = doc.get("clock_s")
    if not isinstance(base, (int, float)):
        return None
    t = float(base)
    if isinstance(entry.get("at"), (int, float)) and written_at is not None:
        t = max(t, base + entry["at"] - written_at)
    return max(t, feed_end) if feed_end is not None else t


def holding(doc: dict, t: float | None, clock: float | None, period: int | None, hold_s: float = CARD_HOLD_S, review: bool = True) -> bool:
    """Whether a card that went up at `t` still holds the screen at `clock`: `hold_s` seconds, or while VAR checks it (`review`), never into a later period."""
    if clock is None or t is None:
        return False
    p = label_period(doc.get("minute_label"))
    if period is not None and p is not None and period > p:
        return False
    return (review and bool(doc.get("under_review"))) or clock < t + hold_s


def var_reason(card: dict) -> str | None:
    """Why VAR ruled the card out ("offside", "handball", ...): `var_decision.reason`, else the desk's `ruled_out`; None without a VAR decision."""
    for source in (card.get("var_decision"), card.get("ruled_out")):
        if isinstance(source, dict):
            reason = str(source.get("reason") or source.get("cause") or "").strip().lower()
            if reason:
                return reason.removeprefix("var_")
    return None


def retraction_message(card: dict, lang: str) -> str:
    """What the lower-third says when a card on air is taken back: "Goal ruled out (VAR, offside)", without the margin."""
    if card.get("moment") != "goal":
        return WITHDRAWN[lang]
    reason = var_reason(card)
    if reason is None:
        return RULED_OUT[lang]
    words = VAR_REASON_WORDS.get(reason, {}).get(lang)
    return f"{RULED_OUT[lang]} (VAR, {words})" if words else f"{RULED_OUT[lang]} (VAR)"


def last_approver(doc: dict) -> str | None:
    """Who approved the card last, from its own history (when there is no audit trail)."""
    return next((h.get("by") for h in reversed(doc.get("history") or []) if isinstance(h, dict) and h.get("action") == "approved"), None)


def _card_level(action: str, doc: dict) -> bool:
    """A card still on air, or the retraction of one that was."""
    return action == "retracted" or (doc.get("type", "card") == "card" and doc.get("status") == "approved")


def on_air(cards: list[dict], audit: list[dict], clock: float | None = None, period: int | None = None) -> dict | None:
    """What the screen shows: `{kind: "card" | "retraction", card, detail}` for the latest approval still standing, or the latest retraction of a card that was on air; `detail` is a fact approved while a card holds the screen. Without `clock` (the match clock of the desk's state), the latest approval wins."""
    by_id = {c["id"]: c for c in cards}
    approver: dict[str, str | None] = {}
    written: dict[str, float] = {}
    feed_end: dict[str, float | None] = {}
    events: list[tuple[str, dict, float | None]] = []
    for e in audit:
        cid, action = e.get("card_id"), e.get("action")
        if action == "written" and isinstance(e.get("at"), (int, float)):
            written.setdefault(cid, e["at"])
        elif not e.get("by") and e.get("minute_label"):  # a Spotter action on the card (hold, release, retract, rewrite): the feed's clock then
            feed_end[cid] = minute_end(e["minute_label"])
        if action == "approved":
            approver[cid] = e.get("by")
        if action == "approved" or (action == "retracted" and e.get("was_on_air")):
            doc = by_id.get(cid)
            if doc is not None:
                events.append((action, doc, went_up(doc, e, written.get(cid), feed_end.get(cid))))

    def holds(action: str, doc: dict, t: float | None) -> bool:
        if action == "retracted":
            return holding(doc, t, clock, period, RETRACTION_HOLD_S, review=False)
        return holding(doc, t, clock, period)

    def held(before: list) -> bool:
        holder = next(((action, doc, t) for action, doc, t in reversed(before) if _card_level(action, doc)), None)
        return holder is not None and holds(*holder)

    for i in range(len(events) - 1, -1, -1):
        action, doc, t = events[i]
        if action == "retracted":
            if clock is not None and t is not None and not holds(action, doc, t):
                return None  # its time is up: the retraction leaves the screen
            return {"kind": "retraction", "card": doc, "detail": None, "auto": False}
        if doc.get("status") != "approved":
            continue
        if doc.get("type") == "insight" and held(events[:i]):
            continue  # a card holds the screen: the fact waits on the detail line
        detail = None
        if doc.get("type") != "insight" and holding(doc, t, clock, period):
            detail = next((d for a, d, _ in reversed(events[i + 1:]) if a == "approved" and d.get("type") == "insight" and d.get("status") == "approved"), None)
        return {"kind": "card", "card": doc, "detail": detail, "auto": approver.get(doc["id"]) == AUTO_APPROVER}
    if events:
        return None
    shown = [c for c in cards if c.get("status") == "approved" or c.get("retracted_on_air")]  # no audit trail: the latest by match clock
    if not shown:
        return None
    card = max(shown, key=lambda c: c.get("clock_s") or 0)
    approved = card["status"] == "approved"
    return {"kind": "card" if approved else "retraction", "card": card, "detail": None, "auto": approved and last_approver(card) == AUTO_APPROVER}


INSIGHT_LABEL = {"en": "The detail", "fr": "Le détail", "es": "El detalle"}


def aireable_insights(insights: list[dict]) -> list[dict]:
    """Insights that can take the lower-third: written (texts) and not superseded. `on_air` then keeps only an approved one."""
    return [d for d in insights if d.get("texts") and d.get("status") != "superseded"]


def text_for(card: dict, lang: str) -> dict | None:
    texts = card.get("texts") or []
    return next((t for t in texts if t.get("lang") == lang), next((t for t in texts if t.get("lang") == "en"), texts[0] if texts else None))


def previous_scorer(card: dict) -> str | None:
    """The scorer of the version before, when the feed credited a goal card to another player (`previous_player`, version 2 or more)."""
    prev = card.get("previous_player")
    if isinstance(prev, dict):
        prev = prev.get("name")
    return prev if isinstance(prev, str) and prev.strip() and int(card.get("version") or 1) >= 2 else None


def params(search: str | None) -> dict:
    q = {k: v[-1] for k, v in parse_qs((search or "").lstrip("?")).items()}
    lang = q.get("lang", "en")
    return {"match": q.get("match"), "lang": lang if lang in LANGS else "en", "clean": q.get("clean") in ("1", "true", "yes")}


def score_bug(state: dict | None) -> html.Div | None:
    if not state or not state.get("teams"):
        return None
    t, s = state["teams"], state.get("score", {"home": 0, "away": 0})
    clock = FULL_TIME if state.get("final") else state.get("minute_label", "")
    return html.Div([html.Span(t["home"], className="ov-team"), html.Span(f"{s['home']}-{s['away']}", className="ov-score"),
                     html.Span(t["away"], className="ov-team"), html.Span(clock, className="ov-clock")], className="ov-bug")


def at_final_whistle(doc: dict, state: dict | None) -> bool:
    """Whether a document on air was written at the final whistle (a fact or a recap of the full time), once the match is over."""
    if not state or not state.get("final"):
        return False
    clock, end = doc.get("clock_s"), state.get("clock_s")
    if isinstance(clock, (int, float)) and isinstance(end, (int, float)) and clock >= end:
        return True
    return doc.get("type", "card") != "card" and bool(doc.get("minute_label")) and doc.get("minute_label") == state.get("minute_label")


def minute_text(doc: dict, state: dict | None) -> str:
    """The minute shown on the lower-third: "FT 5-2" for an element of the final whistle, else its minute."""
    if at_final_whistle(doc, state):
        s = state.get("score") or {}
        return f"{FULL_TIME} {s.get('home', 0)}-{s.get('away', 0)}"
    return doc.get("minute_label", "")


def lower_third(item: dict | None, lang: str, state: dict | None = None) -> html.Div:
    """The lower-third of an `on_air` item; `state` (the match state) gives the final score at the final whistle."""
    if item is None:
        return html.Div(className="ov-lower ov-empty")
    card = item["card"]
    minute = html.Span(minute_text(card, state), className="ov-minute")
    if item["kind"] == "retraction":
        message = retraction_message(card, lang)
        t = text_for(card, lang)
        return html.Div([html.Div([minute, html.Span(message, className="ov-headline")], className="ov-line"),
                         html.Div(t["headline"] if t else "", className="ov-why ov-struck")], className="ov-lower ov-retraction")
    t = text_for(card, lang) or {"headline": "", "why": "", "lang": lang}
    tags = []
    if card.get("type") == "insight":
        tags.append(html.Span(INSIGHT_LABEL[lang], className="ov-tag ov-detail"))
    if previous_scorer(card):
        tags.append(html.Span(SCORER_CORRECTED[lang], className="ov-tag ov-corrected"))
    if card.get("under_review"):
        tags.append(html.Span(VAR_LABEL[lang], className="ov-tag ov-var"))
    if item.get("auto"):
        tags.append(html.Span(AUTO_APPROVED_LABEL[lang], className="ov-tag ov-auto"))
    if t.get("lang") in (card.get("machine_translated") or []):
        tags.append(html.Span(AI_WRITTEN_LABEL[lang], className="ov-tag ov-ai"))
    lines = [html.Div([minute, html.Span(t["headline"], className="ov-headline"), *tags], className="ov-line"), html.Div(t["why"], className="ov-why")]
    extra = text_for(item["detail"], lang) if item.get("detail") else None
    if extra:
        lines.append(html.Div([html.Span(INSIGHT_LABEL[lang], className="ov-tag ov-detail"), html.Span(extra["headline"], className="ov-second-text")], className="ov-second",
                              **{"data-detail": item["detail"]["id"]}))
    return html.Div(lines, className="ov-lower " + ("ov-insight" if card.get("type") == "insight" else f"ov-{card.get('team', 'home')}"),
                    **{"data-card": card["id"], "data-kind": "insight" if card.get("type") == "insight" else item["kind"]})


def render(search: str | None, store=None) -> list:
    """The whole overlay for a query string."""
    store = store or get_store()
    p = params(search)
    match_id = p["match"] or next((m["match_id"] for m in store.matches()), None)
    if match_id is None:
        return [html.Div(className="ov-lower ov-empty")]
    state = store.state(match_id)
    item = on_air(store.cards(match_id) + aireable_insights(store.insights(match_id)), store.audit(match_id), (state or {}).get("clock_s"), (state or {}).get("period"))
    out = [score_bug(state), lower_third(item, p["lang"], state)]
    if not p["clean"]:
        links = [dcc.Link(lang.upper(), href="/overlay?" + urlencode({"match": match_id, "lang": lang}), className="ov-lang" + (" ov-lang-on" if lang == p["lang"] else ""))
                 for lang in LANGS]
        out.insert(0, html.Nav(links, className="ov-langs"))
    return [c for c in out if c is not None]


def layout() -> html.Div:
    return html.Div([dcc.Interval(id="ov-tick", interval=1000), html.Div(id="ov-root", className="ov-root")], className="ov-page")


def register(app) -> None:
    app.callback(Output("ov-root", "children"), Input("ov-tick", "n_intervals"), Input("url", "search"))(lambda _n, search: render(search))
