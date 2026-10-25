"""The public overlay (`/overlay`): a read-only lower-third with the card on air, and a visible retraction when the feed takes back a card that was on air.

A card holds the lower-third for `CARD_HOLD_S` seconds of match clock after it goes up, and while VAR checks it: a fact (insight) approved meanwhile, often in the same feed message as the goal, waits on a detail line under the card and takes the screen once the card's time is up. The retraction of a card that was on air holds the screen the same way.

Query string: `match` (default: the first match), `lang` (`en`, `fr`, `es`), `clean=1` to hide the language links (for a video mixer). This module reads the store and nothing else: no Writer, no model, no producer decision, and never the VAR margin.
"""

import re
from urllib.parse import parse_qs, urlencode

from dash import Input, Output, dcc, html

from web.store import get_store

LANGS = ("en", "fr", "es")
MACHINE_TRANSLATED_LABEL = {"en": "machine-translated", "fr": "traduction automatique", "es": "traducción automática"}
VAR_LABEL = {"en": "VAR check", "fr": "Vérification VAR", "es": "Revisión VAR"}
SCORER_CORRECTED = {"en": "Scorer corrected", "fr": "Buteur corrigé", "es": "Gol reasignado"}
RETRACTION = {
    "goal": {"en": "Goal ruled out: card withdrawn", "fr": "But refusé : info retirée", "es": "Gol anulado: información retirada"},
    "other": {"en": "Card withdrawn", "fr": "Info retirée", "es": "Información retirada"},
}
CARD_HOLD_S = 25.0  # match seconds a card keeps the lower-third after it goes up


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


def holding(doc: dict, t: float | None, clock: float | None, period: int | None) -> bool:
    """Whether a card that went up at `t` still holds the screen at `clock`: CARD_HOLD_S seconds, or while VAR checks it, never into a later period."""
    if clock is None or t is None:
        return False
    p = label_period(doc.get("minute_label"))
    if period is not None and p is not None and period > p:
        return False
    return bool(doc.get("under_review")) or clock < t + CARD_HOLD_S


def _card_level(action: str, doc: dict) -> bool:
    """A card still on air, or the retraction of one that was."""
    return action == "retracted" or (doc.get("type", "card") == "card" and doc.get("status") == "approved")


def on_air(cards: list[dict], audit: list[dict], clock: float | None = None, period: int | None = None) -> dict | None:
    """What the screen shows: `{kind: "card" | "retraction", card, detail}` for the latest approval still standing, or the latest retraction of a card that was on air; `detail` is a fact approved while a card holds the screen. Without `clock` (the match clock of the desk's state), the latest approval wins."""
    by_id = {c["id"]: c for c in cards}
    written: dict[str, float] = {}
    feed_end: dict[str, float | None] = {}
    events: list[tuple[str, dict, float | None]] = []
    for e in audit:
        cid, action = e.get("card_id"), e.get("action")
        if action == "written" and isinstance(e.get("at"), (int, float)):
            written.setdefault(cid, e["at"])
        elif not e.get("by") and e.get("minute_label"):  # a Spotter action on the card (hold, release, retract, rewrite): the feed's clock then
            feed_end[cid] = minute_end(e["minute_label"])
        if action == "approved" or (action == "retracted" and e.get("was_on_air")):
            doc = by_id.get(cid)
            if doc is not None:
                events.append((action, doc, went_up(doc, e, written.get(cid), feed_end.get(cid))))

    def held(before: list) -> bool:
        holder = next(((doc, t) for action, doc, t in reversed(before) if _card_level(action, doc)), None)
        return holder is not None and holding(holder[0], holder[1], clock, period)

    for i in range(len(events) - 1, -1, -1):
        action, doc, t = events[i]
        if action == "retracted":
            return {"kind": "retraction", "card": doc, "detail": None}
        if doc.get("status") != "approved":
            continue
        if doc.get("type") == "insight" and held(events[:i]):
            continue  # a card holds the screen: the fact waits on the detail line
        detail = None
        if doc.get("type") != "insight" and holding(doc, t, clock, period):
            detail = next((d for a, d, _ in reversed(events[i + 1:]) if a == "approved" and d.get("type") == "insight" and d.get("status") == "approved"), None)
        return {"kind": "card", "card": doc, "detail": detail}
    if events:
        return None
    shown = [c for c in cards if c.get("status") == "approved" or c.get("retracted_on_air")]  # no audit trail: the latest by match clock
    if not shown:
        return None
    card = max(shown, key=lambda c: c.get("clock_s") or 0)
    return {"kind": "card" if card["status"] == "approved" else "retraction", "card": card, "detail": None}


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
    return html.Div([html.Span(t["home"], className="ov-team"), html.Span(f"{s['home']}-{s['away']}", className="ov-score"),
                     html.Span(t["away"], className="ov-team"), html.Span(state.get("minute_label", ""), className="ov-clock")], className="ov-bug")


def lower_third(item: dict | None, lang: str) -> html.Div:
    if item is None:
        return html.Div(className="ov-lower ov-empty")
    card = item["card"]
    minute = html.Span(card.get("minute_label", ""), className="ov-minute")
    if item["kind"] == "retraction":
        message = RETRACTION["goal" if card.get("moment") == "goal" else "other"][lang]
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
    if t.get("lang") in (card.get("machine_translated") or []):
        tags.append(html.Span(MACHINE_TRANSLATED_LABEL[lang], className="ov-tag"))
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
    out = [score_bug(state), lower_third(item, p["lang"])]
    if not p["clean"]:
        links = [dcc.Link(lang.upper(), href="/overlay?" + urlencode({"match": match_id, "lang": lang}), className="ov-lang" + (" ov-lang-on" if lang == p["lang"] else ""))
                 for lang in LANGS]
        out.insert(0, html.Nav(links, className="ov-langs"))
    return [c for c in out if c is not None]


def layout() -> html.Div:
    return html.Div([dcc.Interval(id="ov-tick", interval=1000), html.Div(id="ov-root", className="ov-root")], className="ov-page")


def register(app) -> None:
    app.callback(Output("ov-root", "children"), Input("ov-tick", "n_intervals"), Input("url", "search"))(lambda _n, search: render(search))
