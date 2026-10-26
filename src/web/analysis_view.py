"""The Analyst's notes on a card (`card["analysis"]`, written by `worker.analysis`) for the console: the lines in the chosen language, who wrote them and whether the Verifier passed them, and, folded, every number with the fact it comes from. Text for the producer and the commentators, never voiced; a goal VAR ruled out shows its notes as withdrawn. Reads the stored document only: no model, no import of the worker.
"""

from dash import html

from web.labels import field_label

TITLE = "Analyst notes"
NEVER_VOICED = "Proposed text, never voiced."
SOURCES = "Where each number comes from"
WITHDRAWN = "Goal ruled out by VAR: these notes are withdrawn, never use them."


def _num(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


def _backed(claim: dict, facts: dict) -> bool:
    v = facts.get(claim.get("source_field"))
    return isinstance(v, (int, float)) and not isinstance(v, bool) and isinstance(claim.get("value"), (int, float)) and float(v) == float(claim["value"])


def sources_table(lines: list[dict], facts: dict, meanings: dict) -> html.Table | html.Div:
    rows = []
    for i, line in enumerate(lines, 1):
        for c in line.get("claims") or [] if isinstance(line, dict) else []:
            if not isinstance(c, dict):
                continue
            field, ok = str(c.get("source_field", "")), _backed(c, facts)
            rows.append(html.Tr([html.Td(_num(c.get("value")), className="claim-value"), html.Td(field_label(field), className="fact-key", title=field),
                                 html.Td(meanings.get(field) or ("" if ok else "not a fact of this move"), className="muted small" if ok else "claim-bad small"),
                                 html.Td(f"note {i}", className="muted small")]))
    if not rows:
        return html.Div("No number in these notes.", className="muted small")
    return html.Table([html.Tbody(rows)], className="claims")


def analysis_block(card: dict | None, lang: str | None = "en") -> html.Div | None:
    """The card's analyst notes in `lang` (English when that language is missing); None when the card has none."""
    doc = (card or {}).get("analysis")
    if not isinstance(doc, dict) or not isinstance(doc.get("texts"), dict):
        return None
    texts = doc["texts"]
    shown = lang if isinstance(texts.get(lang), list) else "en"
    lines = [l for l in texts.get(shown) or [] if isinstance(l, dict) and isinstance(l.get("line"), str)]
    retracted = doc.get("status") == "retracted"
    head = [html.H3(TITLE), html.P(NEVER_VOICED, className="muted small")]
    if retracted:
        head.append(html.Div(WITHDRAWN, className="correction muted"))
    notes = html.Ul([html.Li(l["line"], className="analysis-line" + (" analysis-withdrawn" if retracted else "")) for l in lines], className="analysis-notes") \
        if lines else html.Div("No note in this language.", className="muted small")
    by = f"Written by {doc.get('writer', '?')}, " + ("verified" if doc.get("verified") else "not verified")
    if shown in (doc.get("machine_translated") or []):
        by += ", machine-translated"
    fallback = (doc.get("verification") or {}).get("fallback_reason")
    if fallback:
        by += f" (template: {fallback})"
    sources = html.Details([html.Summary(SOURCES), sources_table(lines, doc.get("facts") or {}, doc.get("field_meanings") or {})], open=False, className="analysis-sources")
    return html.Div(head + [notes, html.Div(by, className="muted small"), sources], className="analysis-block")
