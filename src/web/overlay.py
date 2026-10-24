"""The public overlay (`/overlay`): a read-only lower-third with the card on air, and a visible retraction when the feed takes back a card that was on air.

Query string: `match` (default: the first match), `lang` (`en`, `fr`, `es`), `clean=1` to hide the language links (for a video mixer). This module reads the store and nothing else: no Writer, no model, no producer decision.
"""

from urllib.parse import parse_qs, urlencode

from dash import Input, Output, dcc, html

from web.store import get_store

LANGS = ("en", "fr", "es")
MACHINE_TRANSLATED_LABEL = {"en": "machine-translated", "fr": "traduction automatique", "es": "traducción automática"}
VAR_LABEL = {"en": "VAR check", "fr": "Vérification VAR", "es": "Revisión VAR"}
RETRACTION = {
    "goal": {"en": "Goal ruled out: card withdrawn", "fr": "But refusé : info retirée", "es": "Gol anulado: información retirada"},
    "other": {"en": "Card withdrawn", "fr": "Info retirée", "es": "Información retirada"},
}


def on_air(cards: list[dict], audit: list[dict]) -> dict | None:
    """What the screen shows: `{kind: "card" | "retraction", card}` for the latest approval still standing, or the latest retraction of a card that was on air."""
    by_id = {c["id"]: c for c in cards}
    events = [e for e in audit if e.get("action") == "approved" or (e.get("action") == "retracted" and e.get("was_on_air"))]
    for e in reversed(events):
        card = by_id.get(e.get("card_id"))
        if card is None:
            continue
        if e["action"] == "retracted":
            return {"kind": "retraction", "card": card}
        if card.get("status") == "approved":
            return {"kind": "card", "card": card}
    if events:
        return None
    shown = [c for c in cards if c.get("status") == "approved" or c.get("retracted_on_air")]  # no audit trail: the latest by match clock
    if not shown:
        return None
    card = max(shown, key=lambda c: c.get("clock_s") or 0)
    return {"kind": "card" if card["status"] == "approved" else "retraction", "card": card}


INSIGHT_LABEL = {"en": "The detail", "fr": "Le détail", "es": "El detalle"}


def aireable_insights(insights: list[dict]) -> list[dict]:
    """Insights that can take the lower-third: written (texts) and not superseded. `on_air` then keeps only an approved one."""
    return [d for d in insights if d.get("texts") and d.get("status") != "superseded"]


def text_for(card: dict, lang: str) -> dict | None:
    texts = card.get("texts") or []
    return next((t for t in texts if t.get("lang") == lang), next((t for t in texts if t.get("lang") == "en"), texts[0] if texts else None))


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
    if card.get("under_review"):
        tags.append(html.Span(VAR_LABEL[lang], className="ov-tag ov-var"))
    if t.get("lang") in (card.get("machine_translated") or []):
        tags.append(html.Span(MACHINE_TRANSLATED_LABEL[lang], className="ov-tag"))
    return html.Div([html.Div([minute, html.Span(t["headline"], className="ov-headline"), *tags], className="ov-line"),
                     html.Div(t["why"], className="ov-why")], className="ov-lower " + ("ov-insight" if card.get("type") == "insight" else f"ov-{card.get('team', 'home')}"),
                    **{"data-card": card["id"], "data-kind": "insight" if card.get("type") == "insight" else item["kind"]})


def render(search: str | None, store=None) -> list:
    """The whole overlay for a query string."""
    store = store or get_store()
    p = params(search)
    match_id = p["match"] or next((m["match_id"] for m in store.matches()), None)
    if match_id is None:
        return [html.Div(className="ov-lower ov-empty")]
    item = on_air(store.cards(match_id) + aireable_insights(store.insights(match_id)), store.audit(match_id))
    out = [score_bug(store.state(match_id)), lower_third(item, p["lang"])]
    if not p["clean"]:
        links = [dcc.Link(lang.upper(), href="/overlay?" + urlencode({"match": match_id, "lang": lang}), className="ov-lang" + (" ov-lang-on" if lang == p["lang"] else ""))
                 for lang in LANGS]
        out.insert(0, html.Nav(links, className="ov-langs"))
    return [c for c in out if c is not None]


def layout() -> html.Div:
    return html.Div([dcc.Interval(id="ov-tick", interval=1000), html.Div(id="ov-root", className="ov-root")], className="ov-page")


def register(app) -> None:
    app.callback(Output("ov-root", "children"), Input("ov-tick", "n_intervals"), Input("url", "search"))(lambda _n, search: render(search))
