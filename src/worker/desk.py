"""The editorial desk: every card (and every insight) is written, checked, then waits for a producer, and the audit trail records who did what.

Statuses: `pending` (written and checked, waiting for a producer), `approved` (on air), `rejected`, `retracted` (the feed took the moment back; a card that was on air must come off). A rewrite (goal credited to another player) writes a new version, which waits for approval again. In auto-approve mode (announced to the judges), a verified card goes on air without a click; a card the Verifier failed and the template replaced is approved too, since the template only uses checked fields.

Each card carries its commentary duo (`duo`), written with the texts. The desk keeps one `PhraseMemory` per match so that nothing is said twice the same way, counts the scorer's goals in the match (`scorer_match_goals`), marks a goal with no stake `on_air_priority: low`, and, when VAR rules a goal out, records why (`ruled_out`: reason, minute, offside margin in centimetres from the card's `var_decision`) and what the duo says about it (`duo_ruling`).

The producers can also judge every sentence (`feedback`: thumbs up or down, reasons, a note). The desk stores it, audits it, and reads the producer profile built from it (`worker.feedback`) before each draft: the Writer prompt gets the control room's preferences, the template writer picks the variants they turned down last.
"""

import asyncio
import time

from worker import feedback as fb
from worker.duo import PhraseMemory
from worker.writer import LANGUAGES, TemplateWriter, card_facts, draft, draft_insight, insight_facts, low_stake, ruling_duo


class Desk:
    def __init__(self, sink, writer=None, auto_approve: bool = False, languages: tuple[str, ...] = LANGUAGES, max_calls_per_match: int = 40,
                 profile=None) -> None:
        self.sink = sink
        self.max_calls_per_match = max_calls_per_match  # cost guard: beyond it, cards use the template
        self.calls: dict[str, int] = {}
        self.writer = writer or TemplateWriter()
        self.auto_approve = auto_approve
        self.languages = list(languages)
        self.loop = asyncio.new_event_loop()
        self.memories: dict[str, PhraseMemory] = {}  # match id -> what the match already said
        self.goals: dict[str, dict[str, dict]] = {}  # match id -> card id -> goal card
        self.profiles = fb.source(profile)  # the producer profile: a dict, a path, or the FEEDBACK_PROFILE file (re-read when it changes)

    def memory(self, match_id: str) -> PhraseMemory:
        return self.memories.setdefault(match_id, PhraseMemory())

    def _audit(self, card: dict, action: str, by: str, detail: dict | None = None) -> None:
        card.setdefault("version", 1)
        self.sink.audit_entry({"id": f"{card['id']}-v{card['version']}-{action}-{len(card.setdefault('history', []))}", "match_id": card["match_id"], "card_id": card["id"],
                               "action": action, "by": by, "version": card["version"], "at": time.time(), **(detail or {})})
        card["history"].append({"action": action, "by": by, "version": card["version"]})

    def _scorer_goals(self, card: dict) -> int:
        """Goals the card's scorer has in the match so far, this one included; goals ruled out do not count."""
        goals = self.goals.setdefault(card["match_id"], {})
        goals[card["id"]] = card
        return 1 + sum(1 for c in goals.values() if c["id"] != card["id"] and c["player"]["id"] == card["player"]["id"] and c.get("status") != "retracted"
                       and c["team"] == card["team"] and c["clock_s"] <= card["clock_s"])

    def _stage(self, doc: dict, d, store) -> dict:
        doc["texts"] = d.texts
        doc["duo"] = d.duo
        doc["machine_translated"] = d.machine_translated
        doc["writer"] = d.writer
        doc["duo_writer"] = d.duo_writer
        doc["verification"] = {"verified": d.report["verified"], "attempts": d.attempts}
        warnings = (d.report.get("repetition") or {}).get("warnings", [])
        if warnings:
            doc["verification"]["repetition_warnings"] = len(warnings)
        self._audit(doc, "written", d.writer, {"verified": d.report["verified"], "attempts": d.attempts})
        if self.auto_approve:
            doc["status"] = "approved"
            self._audit(doc, "approved", "auto-approve")
        else:
            doc["status"] = "pending"
        store(doc)
        return doc

    def _preferences(self, doc: dict) -> dict:
        profile = self.profiles.get()
        return {"preferences": fb.prompt_block(profile, doc, self.languages), "demoted": fb.demoted(profile)}

    def _calls_left(self, match_id: str) -> int:
        return max(self.max_calls_per_match - self.calls.get(match_id, 0), 0)

    def write(self, card: dict, teams: dict[str, str]) -> dict:
        """Writes (or rewrites) a card with its duo and puts it up for approval."""
        if card["moment"] == "goal":
            card["scorer_match_goals"] = self._scorer_goals(card)
        card["on_air_priority"] = "low" if low_stake(card) else "normal"
        d = self.loop.run_until_complete(draft(card, teams, self.writer, self.languages, calls_left=self._calls_left(card["match_id"]),
                                               memory=self.memory(card["match_id"]), duo=True, **self._preferences(card)))
        self.calls[card["match_id"]] = self.calls.get(card["match_id"], 0) + d.attempts
        return self._stage(card, d, self.sink.card)

    def write_insight(self, doc: dict, teams: dict[str, str]) -> dict:
        """Writes an insight (or the storyline) through the same Writer, Verifier, retry and template path as a card, with the same statuses and audit."""
        doc.setdefault("version", 1)
        d = self.loop.run_until_complete(draft_insight(doc, teams, self.writer, self.languages, calls_left=self._calls_left(doc["match_id"]),
                                                       memory=self.memory(doc["match_id"]), duo=True, **self._preferences(doc)))
        self.calls[doc["match_id"]] = self.calls.get(doc["match_id"], 0) + d.attempts
        return self._stage(doc, d, getattr(self.sink, "insight", None) or self.sink.card)

    def on_action(self, card: dict, action: dict, teams: dict[str, str]) -> dict:
        if action["action"] == "retract":
            was_on_air = action.get("previous_status") == "approved"
            card["status"] = "retracted"
            card["retracted_on_air"] = was_on_air
            cause = str(action.get("cause", ""))
            if card["moment"] == "goal" and cause.startswith("var_"):
                margin_m = (card.get("var_decision") or {}).get("margin_m")
                card["ruled_out"] = {"reason": cause[4:], "cause": cause, "minute_label": action.get("minute_label"),
                                     "margin_cm": round(100 * margin_m) if isinstance(margin_m, (int, float)) else None}
                card["duo_ruling"], _ = ruling_duo(card, teams, self.languages, self.memory(card["match_id"]))
            self._audit(card, "retracted", "feed", {"cause": action["cause"], "was_on_air": was_on_air})
        elif action["action"] == "rewrite":
            self._audit(card, "rewrite_requested", "feed", {"cause": action["cause"]})
            return self.write(card, teams)
        else:  # hold, release: the card stays where it is, flagged
            self._audit(card, action["action"], "feed", {"cause": action["cause"]})
        self.sink.card(card)
        return card

    # --- producer decisions (called by the console) ---------------------------------------------

    def approve(self, card: dict, by: str, texts: list[dict] | None = None) -> dict:
        if card["status"] == "retracted":
            raise ValueError("a retracted card cannot go on air")
        if texts is not None:
            card["texts"] = texts
            self._audit(card, "edited", by)
        card["status"] = "approved"
        self._audit(card, "approved", by)
        self.sink.card(card)
        return card

    def reject(self, card: dict, by: str) -> dict:
        card["status"] = "rejected"
        self._audit(card, "rejected", by)
        self.sink.card(card)
        return card

    def feedback(self, doc: dict, target: dict, verdict: str, reasons: list[str] | None, note: str | None, by: str, teams: dict[str, str] | None = None) -> dict:
        """A producer's judgement on one sentence of a card, insight or storyline (`target`: `{part: headline|why|duo, lang, index}`), with the exact text judged, its writer and the facts it was written from. Stored as a `feedback` document and audited; the judged document is not rewritten (its status, texts and history stay as they are)."""
        doc.setdefault("version", 1)
        teams = teams or {"home": "Home", "away": "Away"}
        try:
            facts, meanings = insight_facts(doc, teams) if doc.get("type") in ("insight", "storyline") else card_facts(doc, teams)
        except (KeyError, TypeError, ValueError):
            facts, meanings = dict(doc.get("headline_facts") or doc.get("facts") or {}), dict(doc.get("field_meanings") or {})
        item = fb.make_doc(doc, target, verdict, reasons, note, by, facts, meanings)
        store = getattr(self.sink, "feedback", None) or self.sink.card  # Cosmos: next to the cards, type "feedback"
        store(item)
        self.sink.audit_entry({"id": f"{item['id']}-{int(item['at'] * 1000)}", "match_id": doc["match_id"], "card_id": doc["id"], "action": "feedback", "by": by,
                               "version": doc["version"], "at": item["at"], "target": item["target"], "verdict": verdict, "reasons": item["reasons"]})
        return item
