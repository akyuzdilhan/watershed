"""The editorial desk: every card (and every insight) is written, checked, then waits for a producer, and the audit trail records who did what.

Statuses: `pending` (written and checked, waiting for a producer), `approved` (on air), `rejected`, `retracted` (the feed took the moment back; a card that was on air must come off). A rewrite (goal credited to another player) writes a new version, which waits for approval again. In auto-approve mode (announced to the judges), a verified card goes on air without a click; a card the Verifier failed and the template replaced is approved too, since the template only uses checked fields.

Each card carries its commentary duo (`duo`), written with the texts. The desk keeps one `PhraseMemory` per match so that nothing is said twice the same way, counts the scorer's goals in the match (`scorer_match_goals`), marks a goal with no stake `on_air_priority: low`, and, when VAR rules a goal out, records why (`ruled_out`: reason, minute, offside margin in centimetres when the feed gives it) and what the duo says about it (`duo_ruling`).
"""

import asyncio
import time

from worker.duo import PhraseMemory
from worker.writer import LANGUAGES, TemplateWriter, draft, draft_insight, low_stake, ruling_duo


class Desk:
    def __init__(self, sink, writer=None, auto_approve: bool = False, languages: tuple[str, ...] = LANGUAGES, max_calls_per_match: int = 40) -> None:
        self.sink = sink
        self.max_calls_per_match = max_calls_per_match  # cost guard: beyond it, cards use the template
        self.calls: dict[str, int] = {}
        self.writer = writer or TemplateWriter()
        self.auto_approve = auto_approve
        self.languages = list(languages)
        self.loop = asyncio.new_event_loop()
        self.memories: dict[str, PhraseMemory] = {}  # match id -> what the match already said
        self.goals: dict[str, dict[str, dict]] = {}  # match id -> card id -> goal card

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

    def _calls_left(self, match_id: str) -> int:
        return max(self.max_calls_per_match - self.calls.get(match_id, 0), 0)

    def write(self, card: dict, teams: dict[str, str]) -> dict:
        """Writes (or rewrites) a card with its duo and puts it up for approval."""
        if card["moment"] == "goal":
            card["scorer_match_goals"] = self._scorer_goals(card)
        card["on_air_priority"] = "low" if low_stake(card) else "normal"
        d = self.loop.run_until_complete(draft(card, teams, self.writer, self.languages, calls_left=self._calls_left(card["match_id"]),
                                               memory=self.memory(card["match_id"]), duo=True))
        self.calls[card["match_id"]] = self.calls.get(card["match_id"], 0) + d.attempts
        return self._stage(card, d, self.sink.card)

    def write_insight(self, doc: dict, teams: dict[str, str]) -> dict:
        """Writes an insight (or the storyline) through the same Writer, Verifier, retry and template path as a card, with the same statuses and audit."""
        doc.setdefault("version", 1)
        d = self.loop.run_until_complete(draft_insight(doc, teams, self.writer, self.languages, calls_left=self._calls_left(doc["match_id"]),
                                                       memory=self.memory(doc["match_id"]), duo=True))
        self.calls[doc["match_id"]] = self.calls.get(doc["match_id"], 0) + d.attempts
        return self._stage(doc, d, getattr(self.sink, "insight", None) or self.sink.card)

    def _offside_margin_cm(self, action: dict) -> int | None:
        """The offside margin of a goal VAR voided: the feed sends it on the pass, in a correction tied to the same VAR decision as the void."""
        events = getattr(self.sink, "events", None)
        if not isinstance(events, list):
            return None
        void = next((e for e in reversed(events) if e.get("message_id") == action.get("message_id")), None)
        related = (void or {}).get("related_message_id")
        if not related:
            return None
        for e in reversed(events):
            offside = (e.get("changes") or {}).get("offside") if e.get("type") == "correction" and e.get("related_message_id") == related else None
            if isinstance(offside, dict) and isinstance(offside.get("margin_m"), (int, float)):
                return round(100 * offside["margin_m"])
        return None

    def on_action(self, card: dict, action: dict, teams: dict[str, str]) -> dict:
        if action["action"] == "retract":
            was_on_air = action.get("previous_status") == "approved"
            card["status"] = "retracted"
            card["retracted_on_air"] = was_on_air
            cause = str(action.get("cause", ""))
            if card["moment"] == "goal" and cause.startswith("var_"):
                card["ruled_out"] = {"reason": cause[4:], "cause": cause, "minute_label": action.get("minute_label"), "margin_cm": self._offside_margin_cm(action)}
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
