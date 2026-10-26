"""The Analyst's notes on a card (`card["analysis"]`, written by `worker.analysis`) for the console: the lines in the chosen language, who wrote them and whether the Verifier passed them ("Written by gpt-5-mini, verified"; the template's reason for taking over stays with the producer), and, folded, every number with the fact it comes from. Text for the producer and the commentators, never voiced; a goal VAR ruled out shows its notes as withdrawn. Reads the stored document only: no model, no import of the worker.
"""

from dash import html

from web.labels import field_label

TITLE = "Analyst notes"
NEVER_VOICED = "Proposed text, never voiced."
SOURCES = "Where the analyst's numbers come from"
TEMPLATE_WRITERS = {"template": "the template (model refused or off)", "dry": "the template (dry run, no model)"}
WITHDRAWN = "Goal ruled out by VAR: these notes are withdrawn, never use them."


def _num(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


def _backed(claim: dict, facts: dict) -> bool:
    v = facts.get(claim.get("source_field"))
    return isinstance(v, (int, float)) and not isinstance(v, bool) and isinstance(claim.get("value"), (int, float)) and float(v) == float(claim["value"])


def writer_line(doc: dict, lang: str) -> str:
    """Who wrote the notes and whether the Verifier passed them: "Written by gpt-5-mini, verified", "Written by the template (model refused or off), verified"."""
    writer = str(doc.get("writer") or "?")
    by = f"Written by {TEMPLATE_WRITERS.get(writer, writer)}, " + ("verified" if doc.get("verified") else "not verified")
    return by + (", machine-translated" if lang in (doc.get("machine_translated") or []) else "")


def sources_rows(lines: list[dict], facts: dict, meanings: dict) -> list:
    rows = []
    for i, line in enumerate(lines, 1):
        for c in line.get("claims") or [] if isinstance(line, dict) else []:
            if not isinstance(c, dict):
                continue
            field, ok = str(c.get("source_field", "")), _backed(c, facts)
            rows.append(html.Tr([html.Td(_num(c.get("value")), className="claim-value"), html.Td(field_label(field), className="fact-key", title=field),
                                 html.Td(meanings.get(field) or ("" if ok else "not a fact of this move"), className="muted small" if ok else "claim-bad small"),
                                 html.Td(f"note {i}", className="muted small")]))
    return rows


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
    rows = sources_rows(lines, doc.get("facts") or {}, doc.get("field_meanings") or {})
    table = html.Table([html.Tbody(rows)], className="claims") if rows else html.Div("No number in these notes.", className="muted small")
    sources = html.Details([html.Summary(f"{SOURCES} ({len(rows)})"), table], open=False, className="fold analysis-sources")
    return html.Div(head + [notes, html.Div(writer_line(doc, shown), className="muted small"), sources], className="analysis-block")
