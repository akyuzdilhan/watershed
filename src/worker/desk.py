"""The editorial desk: every card is written, checked, then waits for a producer, and the audit trail records who did what.

Statuses: `pending` (written and checked, waiting for a producer), `approved` (on air), `rejected`, `retracted` (the feed took the moment back; a card that was on air must come off). A rewrite (goal credited to another player) writes a new version, which waits for approval again. In auto-approve mode (announced to the judges), a verified card goes on air without a click; a card the Verifier failed and the template replaced is approved too, since the template only uses checked fields.
"""

import asyncio
import time

from worker.writer import LANGUAGES, TemplateWriter, draft


class Desk:
    def __init__(self, sink, writer=None, auto_approve: bool = False, languages: tuple[str, ...] = LANGUAGES) -> None:
        self.sink = sink
        self.writer = writer or TemplateWriter()
        self.auto_approve = auto_approve
        self.languages = list(languages)
        self.loop = asyncio.new_event_loop()

    def _audit(self, card: dict, action: str, by: str, detail: dict | None = None) -> None:
        self.sink.audit_entry({"id": f"{card['id']}-v{card['version']}-{action}-{len(card.setdefault('history', []))}", "match_id": card["match_id"], "card_id": card["id"],
                               "action": action, "by": by, "version": card["version"], "at": time.time(), **(detail or {})})
        card["history"].append({"action": action, "by": by, "version": card["version"]})

    def write(self, card: dict, teams: dict[str, str]) -> dict:
        """Writes (or rewrites) a card and puts it up for approval."""
        d = self.loop.run_until_complete(draft(card, teams, self.writer, self.languages))
        card["texts"] = d.texts
        card["machine_translated"] = d.machine_translated
        card["writer"] = d.writer
        card["verification"] = {"verified": d.report["verified"], "attempts": d.attempts}
        self._audit(card, "written", d.writer, {"verified": d.report["verified"], "attempts": d.attempts})
        if self.auto_approve:
            card["status"] = "approved"
            self._audit(card, "approved", "auto-approve")
        else:
            card["status"] = "pending"
        self.sink.card(card)
        return card

    def on_action(self, card: dict, action: dict, teams: dict[str, str]) -> dict:
        if action["action"] == "retract":
            was_on_air = action.get("previous_status") == "approved"
            card["status"] = "retracted"
            card["retracted_on_air"] = was_on_air
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

