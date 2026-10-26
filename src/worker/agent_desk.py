"""The desk with the commentary duo written by two agents (`python -m worker ... --duo agents`).

A card is written, checked and put on the desk exactly as the plain `Desk` does it, with the template duo, so the duo never delays the card. For a card worth it, the Writer is asked for the texts only, and a job for Wren and Joss starts on the duo's thread (`worker.duo_agents`). When the job passes the Verifier, its duo replaces the template duo at the pipeline's next message, an audit entry says so, and the texts and the producer's decisions stay as they are; when it does not, the template duo stays, with an audit entry giving the reason. Everything else is the Desk.
"""

import copy
import logging
from collections import Counter

from worker.desk import Desk
from worker.duo import EYE, HEART
from worker.duo_agents import DUO_MOMENTS, JOSS_TOOLS, DuoResult, check_duo, duo_spec, turns_line, turns_needed
from worker.mcp_server import match_state_facts, storyline_facts
from worker.pipeline import PRODUCER_FIELDS
from worker.writer import TemplateWriter, accepts, card_facts, low_stake

log = logging.getLogger(__name__)


class TextsOnly:
    """A Writer asked for the card texts only: write_async has no duo parameter, so draft() attaches the template duo; the agents write theirs after. The trace
    of the Writer's calls (last_call, config, max_tokens) is the Writer's own."""

    def __init__(self, writer) -> None:
        self.writer = writer
        self.name = writer.name

    @property
    def last_call(self):
        return getattr(self.writer, "last_call", None)

    @last_call.setter
    def last_call(self, value) -> None:
        if hasattr(self.writer, "last_call"):
            self.writer.last_call = value

    @property
    def config(self):
        return getattr(self.writer, "config", None)

    @property
    def max_tokens(self):
        return getattr(self.writer, "max_tokens", None)

    async def write_async(self, card: dict, facts: tuple[dict, dict], languages: list[str], note: str | None = None, avoid: dict | None = None,
                          preferences: str | None = None, max_tokens: int | None = None) -> str:
        kwargs = {}
        if note is not None:
            kwargs["note"] = note
        for name, value in (("avoid", avoid), ("preferences", preferences), ("max_tokens", max_tokens)):
            if value is not None and accepts(self.writer, name):
                kwargs[name] = value
        return await self.writer.write_async(card, facts, languages, **kwargs)


class AgentDesk(Desk):
    """The Desk, with the duo of eligible cards handed to Wren and Joss after the card is on the desk (`runner`: a worker.duo_agents.DuoRunner; `registry`: the FactRegistry its MCP server reads)."""

    def __init__(self, sink, writer=None, *, runner, registry, min_value: int = 10, **kwargs) -> None:
        super().__init__(sink, writer, **kwargs)
        self.runner = runner
        self.registry = registry
        self.min_value = min_value  # value_pts below this: the template duo is enough
        self.live: dict[str, dict] = {}  # card id -> the card as written, until its match is over and its jobs have ended
        self.teams: dict[str, dict[str, str]] = {}  # match id -> teams
        self._over: set[str] = set()  # matches with a recap, to forget once their jobs have ended
        self.applied = 0
        self.fallbacks: Counter = Counter()

    def duo_eligible(self, card: dict) -> bool:
        """A moment the agents may comment: not a goal with no stake, and worth min_value points or more."""
        return card.get("moment") in DUO_MOMENTS and not low_stake(card) and (card.get("value_pts") or 0) >= self.min_value

    def write(self, card: dict, teams: dict[str, str]) -> dict:
        """Writes (or rewrites) a card as the Desk does; for an eligible card while the duo has free runs for every turn its job may need (turns_needed), the Writer writes the texts only and the agents' job is submitted once the card is stored. A hand-over that fails leaves the card its template duo and never stops the pipeline."""
        self.runner.cancel(card["id"])
        self.teams[card["match_id"]] = dict(teams)
        self._over.discard(card["match_id"])
        card.pop("duo_trace", None)  # a rewritten card gets the trace of its own job, never the one of the version before
        eligible = self.duo_eligible(card)
        if eligible and card["moment"] == "goal":
            card["scorer_match_goals"] = self._scorer_goals(card)  # as Desk.write sets it: a hat-trick takes a third turn
        candidate = eligible and self.runner.budget.left(card["match_id"]) >= turns_needed(card_facts(card, teams)[0])
        writer = self.writer
        if candidate and not isinstance(writer, TemplateWriter):
            self.writer = TextsOnly(writer)
        try:
            super().write(card, teams)
        finally:
            self.writer = writer
        self.live[card["id"]] = card
        try:
            self._hand_over(card, teams, candidate, eligible)
        except Exception as error:
            log.exception("duo %s v%s not handed over", card["id"], card.get("version", 1))
            if eligible:
                self._safe_fallback(card, f"error:{type(error).__name__}")
        return card

    def _hand_over(self, card: dict, teams: dict[str, str], candidate: bool, eligible: bool) -> None:
        """What the agents read through MCP (the card's facts, the match so far), then the job, with the runs of its turns reserved."""
        match_id = card["match_id"]
        self.registry.put_moment(card["id"], *card_facts(card, teams))
        cards = [c for c in self.live.values() if c["match_id"] == match_id]
        self.registry.put_state(card["id"], *match_state_facts(cards, card, teams))
        if not candidate:
            if eligible:
                self._fallback(card, "budget")
            return
        tools = {HEART: ("get_match_state",) + (("get_storyline",) if self.registry.storyline(match_id) else ()),
                 EYE: JOSS_TOOLS if self.runner.history_facts is not None else ()}
        spec = duo_spec(card, teams, self.languages, self.memory(match_id), tools)
        job = (spec.card_id, spec.version)
        if not self.runner.budget.reserve(match_id, job, len(spec.plan)):
            self._fallback(card, "budget")
            return
        try:
            self.runner.submit(spec)
        except Exception:
            self.runner.budget.release(match_id, job)
            raise

    def write_insight(self, doc: dict, teams: dict[str, str]) -> dict:
        """As the Desk; the storyline also goes to the registry, for Wren's get_storyline."""
        out = super().write_insight(doc, teams)
        if doc.get("type") == "storyline":
            self.registry.put_storyline(doc["match_id"], *storyline_facts(doc))
        return out

    def on_action(self, card: dict, action: dict, teams: dict[str, str]) -> dict:
        """As the Desk; a retraction or a rewrite first cancels the card's job (a rewrite writes the new version, which gets its own job)."""
        if action.get("action") in ("retract", "rewrite"):
            self.runner.cancel(card["id"])
        return super().on_action(card, action, teams)

    def write_recap(self, cards: list[dict], state: dict, storyline: dict | None = None) -> dict:
        """As the Desk; the match is then forgotten by the duo once its jobs have ended (poll), until a card of it is written again."""
        doc = super().write_recap(cards, state, storyline)
        self._over.add(state["match_id"])
        return doc

    def poll(self) -> int:
        """Applies the jobs finished since the last call, on the caller's thread (the pipeline's); returns how many duos were applied. A duo that cannot be applied is logged and never stops the pipeline. A match over with no job left is forgotten after."""
        done = [m for m in self._over if not self.runner.pending(m)]  # before results(): a job ends after its result is queued
        applied = 0
        for result in self.runner.results():
            try:
                applied += self._apply(result)
            except Exception:
                log.exception("duo %s v%s not applied", result.card_id, result.version)
        for match_id in done:
            self._forget(match_id)
        return applied

    def _forget(self, match_id: str) -> None:
        ids = [i for i, c in self.live.items() if c["match_id"] == match_id]
        for i in ids:
            del self.live[i]
        self.teams.pop(match_id, None)
        self.registry.drop(ids, match_id)
        self._over.discard(match_id)

    def close(self, timeout: float | None = None) -> None:
        """Waits for the duo jobs (`timeout` seconds at most), stops the runner, and applies what finished."""
        self.runner.close(timeout)
        self.poll()

    def _refresh(self, card: dict) -> None:
        """Takes the producer's decisions from the store before writing the card again, as the pipeline does."""
        load = getattr(self.sink, "load_card", None)
        stored = load(card["match_id"], card["id"]) if load else None
        if stored is not None and stored is not card:
            for key in PRODUCER_FIELDS:
                if key in stored:
                    card[key] = copy.deepcopy(stored[key])

    def _apply(self, result: DuoResult) -> bool:
        card = self.live.get(result.card_id)
        if card is None or card.get("version", 1) != result.version:
            log.debug("duo %s v%s dropped: the card has another version", result.card_id, result.version)
            return False
        self._refresh(card)
        teams = self.teams.get(card["match_id"]) or {}
        reason = result.reason
        if result.ok:
            if card.get("status") in ("retracted", "rejected"):
                reason = card["status"]
            else:
                facts = card_facts(card, teams)[0] | {k: v["value"] for k, v in result.tool_facts.items()}
                if check_duo(result.lines, self.languages, facts, [tuple(p) for p in result.trace.get("plan", [])], result.subjects):
                    reason = "final"
        trace = dict(result.trace, applied=reason is None, reason=reason)
        model = f"agents:{self.runner.model.name}"
        if reason is None:
            memory = self.memory(card["match_id"])
            _, warnings = memory.issues(card["id"], [], result.lines)
            trace["repetition_warnings"] = len(warnings)
            card["duo"] = result.lines
            card["duo_writer"] = model
            card["duo_trace"] = trace
            memory.record(card["id"], card.get("texts") or [], result.lines, card_facts(card, teams)[0])
            tools = [f"{t['name']}({', '.join(str(v) for v in t['args'].values())})" for turn in trace["turns"] for t in turn.get("tools") or []]
            self._audit(card, "duo_written", model, {"cause": turns_line(trace), "turns": len(trace["turns"]), "runs": trace["runs"], "ms": trace["ms"], "tools": tools})
            self.applied += 1
        else:
            card["duo_trace"] = trace
            self._audit(card, "duo_fallback", model, {"cause": reason, "reason": reason, "kept": card.get("duo_writer")})
            self.fallbacks[reason] += 1
        self.sink.card(card)
        return reason is None

    def _fallback(self, card: dict, reason: str) -> None:
        """The duo written with the card stays without a job (the duo's budget is spent, or the hand-over failed): the trace and the audit say why, and `kept` who wrote that duo (the template, or the Writer when the budget was spent before it was asked for the texts only)."""
        model = f"agents:{self.runner.model.name}"
        card["duo_trace"] = {"model": self.runner.model.name, "plan": [], "turns": [], "runs": 0, "ms": 0, "tool_facts": {}, "conflicts": [], "applied": False,
                             "reason": reason}
        self._audit(card, "duo_fallback", model, {"cause": reason, "reason": reason, "kept": card.get("duo_writer")})
        self.fallbacks[reason] += 1
        self.sink.card(card)

    def _safe_fallback(self, card: dict, reason: str) -> None:
        try:
            self._fallback(card, reason)
        except Exception:
            log.exception("duo %s v%s: fallback not recorded", card["id"], card.get("version", 1))
