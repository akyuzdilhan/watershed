"""The insight engine: facts about the match in progress, measured against the match itself and the league history, ranked by how rare they are.

After each relevant message (goal, red card, substitution, VAR check, period end, full time, correction) every detector looks at the match as it now stands (goals that count, scorers after reassignment, substitutes, red cards, shots) and proposes candidate facts. Each fact has a `group` (what it is about: one scorer, one team's comeback) and a `level` (how far it went: a brace, a hat-trick). The engine then reconciles the candidates with the insights already written:

- a new group becomes a `proposed` insight if its value reaches `min_value` and the bucket (first half, second half, full time) has fewer than `cap_per_period` new insights; beyond the cap it replaces the weakest insight still waiting for a producer, if it is worth more;
- a higher (or, after a correction, lower) level in a group supersedes the previous insight (`status: superseded`, `superseded_by`);
- a group that is no longer true (its goal was ruled out) is superseded with no successor;
- an insight whose goals are under a VAR check is flagged `under_review`.

Every fact is deterministic: the same feed and the same history give the same insights. Every number a text may show is in `headline_facts`, with its meaning in `field_meanings` (contract, "Insight"). At full time the engine adds a "stat of the match" insight: the top fact, why it ranks first, and a short ranked list.

The detectors are listed in `DETECTORS`; the rarity method is in `worker.insight_rarity`, the history view in `worker.insight_history`, the catalogue and a worked example in `docs/notes/insights.md`.
"""

import copy
from dataclasses import dataclass, field
from typing import Callable

from worker.insight_history import LATE_MINUTE, Features, HistoryView, comeback_stage
from worker.insight_rarity import binom_sf, bits, fmt, laplace, normal_sf, one_in, poisson_sf, value
from worker.feedback import adjust_value
from worker.feedback import source as profile_source
from worker.match import Match

BY = "insights"
PRODUCER_FIELDS = ("status", "texts", "history", "machine_translated", "writer", "verification")
ACTIVE = ("proposed", "pending", "approved")
STAGES = {1: "level", 2: "ahead", 3: "won"}


@dataclass
class Query:
    """A count over the history: `fn(match) -> (occurrences, opportunities)`, and the match-level predicate `History.last` is asked with."""
    name: str
    fn: Callable[[Features], tuple[int, int]] | None
    what: str  # "4 or more goals by a substitute"
    unit: str  # "matches", "teams that trailed by 2"
    pred: Callable[[Features], bool] | None = None
    who: Callable[[Features], str | None] | None = None  # the player of the last occurrence
    counts: tuple[int, int] | None = None  # already counted (runs, droughts): (occurrences, opportunities)


@dataclass
class Fact:
    group: str
    level: str
    kind: str  # record, rarity, streak, milestone, storyline, tactical
    facts: dict
    meanings: dict
    relevance: float
    event_id: str | None = None
    minute_label: str | None = None
    clock_s: float | None = None
    evidence: list[str] = field(default_factory=list)  # goals the fact counts
    query: Query | None = None
    model_p: float | None = None  # a model probability, for facts measured against the match itself
    model_basis: str | None = None

    @property
    def key(self) -> str:
        return f"{self.group}-{self.level}"


@dataclass
class LiveGoal:
    event_id: str
    seq: int
    side: str
    player: dict
    period: int
    clock_s: float
    minute: int  # match minute as shown, 1 to 90+
    minute_label: str


def other(side: str) -> str:
    return "away" if side == "home" else "home"


def shown_minute(a: dict) -> int:
    return int(a["minute"]) + 1


class InsightEngine:
    def __init__(self, match: Match, view: HistoryView, spotter=None, cap_per_period: int = 4, min_value: int = 40, load: Callable | None = None, profile=None) -> None:
        self.profiles = profile_source(profile)  # the producers' approval rate per family weighs the value (worker.feedback.adjust_value)
        self.match = match
        self.view = view
        self.spotter = spotter
        self.cap = cap_per_period
        self.min_value = min_value
        self.load = load  # (match_id, doc_id) -> stored doc, for the producer's decisions
        self.docs: dict[str, dict] = {}  # key -> insight
        self.current: dict[str, str] = {}  # group -> key of its active insight
        self.bucket_of: dict[str, object] = {}
        self.admitted: dict[object, int] = {}
        self.admitted_groups: set[str] = set()  # a group admitted once is not capped again when it comes back
        self.subs: dict[str, dict[str, tuple[int, int, str]]] = {"home": {}, "away": {}}  # side -> player id -> (seq, minute, label)
        self.reviewing: set[str] = set()
        self._now: dict = {}
        self.half_time = False
        self.ranked: list[tuple[int, float, Fact, dict]] = []
        self.weights: dict[str, float] = {}  # group -> feedback weight, when it is not 1

    # --- intake -----------------------------------------------------------------------------------

    @property
    def final(self) -> bool:
        return self.match.full_time_seq is not None

    def relevant(self, m: dict) -> bool:
        t = m["type"]
        if t in ("substitution", "period_end", "full_time", "correction", "var_check"):
            return True
        return t == "action" and (m["event_id"] in self.match.goals or m["event_id"] in self.match.red_cards)

    def process(self, m: dict) -> list[tuple[dict, bool]]:
        """What message `m` changes: (insight, needs_text) pairs, superseded ones first. `needs_text` is set when the facts are new or changed."""
        t = m["type"]
        if t == "substitution" and m.get("player_in"):
            self.subs[m["team"]][m["player_in"]["id"]] = (m["seq"], shown_minute(m), m["minute_label"])
        if (t == "period_end" and m.get("period") == 1) or m.get("period", 1) >= 2:
            self.half_time = True
        if not self.relevant(m):
            return []
        out: list[tuple[dict, bool]] = []
        if t == "var_check":
            target = m["target_event_id"]
            if m["status"] == "started":
                self.reviewing.add(target)
            else:
                self.reviewing.discard(target)
            for key in self.current.values():
                doc = self.docs[key]
                flag = any(e in self.reviewing for e in doc["evidence"])
                if target in doc["evidence"] and doc["under_review"] != flag:
                    doc["under_review"] = flag
                    out.append((doc, False))
            return out
        cause = "goal ruled out" if t == "correction" and m.get("op") == "void" else "correction" if t == "correction" else None
        out += self._reconcile(self.detect(), m, cause)
        if self.final:
            out += self._stat_of_the_match(m)
        seen, unique = set(), []
        for doc, needs in reversed(out):
            if doc["id"] in seen:
                continue
            seen.add(doc["id"])
            unique.append((doc, needs or any(d is doc and n for d, n in out)))
        return list(reversed(unique))

    # --- live state -------------------------------------------------------------------------------

    def goals(self) -> list[LiveGoal]:
        out = []
        for eid, (seq, side) in sorted(self.match.goals.items(), key=lambda x: x[1][0]):
            a = self.match.versions[eid]
            out.append(LiveGoal(eid, seq, side, a["player"], a["period"], a["clock_s"], shown_minute(a), a["minute_label"]))
        return out

    def teams(self) -> dict[str, str]:
        return {side: t["name"] for side, t in self.match.info["teams"].items()}

    def card_value(self, event_id: str | None) -> float:
        card = self.spotter.cards.get(event_id) if self.spotter is not None and event_id else None
        return min(1.0, card["value_pts"] / 40) if card else 0.3

    def sub_of(self, side: str, pid: str, before_seq: int) -> tuple[int, int, str] | None:
        s = self.subs[side].get(pid)
        return s if s is not None and s[0] < before_seq else None

    def actions(self, period: int | None = None):
        for a in self.match.versions.values():
            if period is None or a.get("period") == period:
                yield a

    def last(self, name: str, pred: Callable[[Features], bool]) -> Features | None:
        return self.view.last(pred, name)

    # --- detectors --------------------------------------------------------------------------------

    def detect(self) -> list[Fact]:
        if self.match.info is None:
            return []
        ctx = {"goals": self.goals(), "teams": self.teams()}
        out = []
        for detector in DETECTORS:
            out += detector(self, ctx)
        return out

    # --- rarity, value, lifecycle -----------------------------------------------------------------

    def rarity(self, f: Fact) -> dict:
        v = self.view
        if f.query is None:
            p = f.model_p if f.model_p is not None else 0.5
            return {"p": p, "surprisal_bits": round(bits(p), 2), "basis": f.model_basis, "occurrences": None, "opportunities": None, "last": None}
        q = f.query
        k, n = q.counts if q.counts is not None else v.count(q.name, q.fn)
        p = laplace(k, n)
        basis = f"{q.what}: {fmt(k)} in {fmt(n)} {q.unit} since {v.first_season}" + (", never before" if k == 0 else "")
        last = None
        if k and q.pred is not None:
            hit = self.view.last(q.pred, q.name)
            if hit is not None:
                last = {"season": hit.season, "match": f"{hit.home} {hit.hg}-{hit.ag} {hit.away}", "player": v.player_name(q.who(hit)) if q.who else None}
                basis += f"; last time {hit.season}, {last['match']}" + (f" ({last['player']})" if last["player"] else "")
        return {"p": p, "surprisal_bits": round(bits(p), 2), "basis": basis, "occurrences": k, "opportunities": n, "last": last}

    def _headline(self, f: Fact, r: dict) -> tuple[dict, dict]:
        facts, meanings = dict(f.facts), dict(f.meanings)
        facts |= {"one_in": one_in(r["p"]), "surprisal_bits": round(r["surprisal_bits"], 1)}
        meanings |= {"one_in": "how rare the fact is: it happens about once in this many opportunities (Laplace-smoothed frequency, or the model probability)",
                     "surprisal_bits": "rarity in bits, -log2 of the probability: one more bit is twice as rare"}
        if r["occurrences"] is not None:
            seasons = len(self.view.seasons)
            facts |= {"history_count": r["occurrences"], "history_opportunities": r["opportunities"], "history_since": self.view.first_season, "history_seasons": seasons}
            facts["history_rank"] = r["occurrences"] + 1
            meanings["history_rank"] = "this one's rank in the league history, counting it: 6 means the sixth time"
            if seasons and r["occurrences"] >= seasons:  # "about 2.3 times a season" speaks to a viewer; "114 in 9,579" does not
                per = r["occurrences"] / seasons
                facts["history_per_season"] = int(round(per)) if per >= 10 else round(per, 1)
                meanings["history_per_season"] = "how many times a season it happens on average in the league history"
            meanings |= {"history_count": f"how many times it happened in the league history ({f.query.what})",
                         "history_opportunities": f"how many {f.query.unit} the history counts",
                         "history_since": "first season of the league history", "history_seasons": "how many seasons the league history covers"}
        if r["last"]:
            facts |= {"last_season": r["last"]["season"], "last_match": r["last"]["match"]}
            meanings |= {"last_season": "season of the last time it happened", "last_match": "the match it last happened in, with its final score"}
            if r["last"]["player"]:
                facts["last_player"] = r["last"]["player"]
                meanings["last_player"] = "player who last did it"
        return facts, meanings

    def _refresh(self, doc: dict) -> None:
        stored = self.load(doc["match_id"], doc["id"]) if self.load else None
        if stored is not None and stored is not doc:
            for k in PRODUCER_FIELDS:
                if k in stored:
                    doc[k] = copy.deepcopy(stored[k])

    def _mark(self, doc: dict, action: str) -> None:
        doc.setdefault("history", []).append({"action": action, "by": BY, "version": doc["version"]})

    def _supersede(self, doc: dict, by: str | None, cause: str) -> None:
        self._refresh(doc)
        doc["status"] = "superseded"
        doc["superseded_by"] = f"insight-{self.match.match_id}-{by}" if by else None
        doc["supersede_cause"] = cause
        self._mark(doc, "superseded")
        self.current.pop(doc["group"], None)

    def _fill(self, doc: dict, f: Fact, r: dict, val: int) -> None:
        facts, meanings = self._headline(f, r)
        doc.update({"kind": f.kind, "minute_label": f.minute_label or self._now.get("minute_label"), "clock_s": f.clock_s if f.clock_s is not None else self._now.get("clock_s"), "event_id": f.event_id, "headline_facts": facts, "field_meanings": meanings,
                    "rarity": {"p": r["p"], "surprisal_bits": r["surprisal_bits"], "basis": r["basis"]}, "last_time": r["last"], "value": val,
                    "evidence": list(f.evidence), "under_review": any(e in self.reviewing for e in f.evidence)})
        w = self.weights.get(f.group)
        if w is None:
            doc.pop("feedback_weight", None)
        else:
            doc["feedback_weight"] = w

    def _reconcile(self, cands: list[Fact], m: dict, cause: str | None) -> list[tuple[dict, bool]]:
        out: list[tuple[dict, bool]] = []
        self._now = m
        bucket = "full_time" if self.final else m.get("period", 1)
        groups = set()
        scored = []
        profile = self.profiles.get()
        self.weights: dict[str, float] = {}
        for f in cands:
            r = self.rarity(f)
            val, w = adjust_value(value(r["surprisal_bits"], f.relevance), f.group, profile, self.min_value)
            if w != 1.0:
                self.weights[f.group] = w
            scored.append((val, r["surprisal_bits"], f, r))
        self.ranked = sorted(scored, key=lambda x: (-x[0], -x[1], x[2].key))
        for val, _, f, r in scored:
            groups.add(f.group)
            key, cur = f.key, self.current.get(f.group)
            if cur == key:
                doc = self.docs[key]
                facts, _ = self._headline(f, r)
                if doc["headline_facts"] != facts or doc["evidence"] != f.evidence:
                    self._refresh(doc)
                    doc["version"] += 1
                    self._fill(doc, f, r, val)
                    if doc["status"] in ACTIVE:
                        doc["status"] = "proposed"
                    self._mark(doc, "updated")
                    out.append((doc, True))
                continue
            if cur is None and f.group not in self.admitted_groups:
                if val < self.min_value:
                    continue
                if self.admitted.get(bucket, 0) >= self.cap:
                    waiting = [self.docs[k] for k in self.current.values() if self.bucket_of.get(k) == bucket and self.docs[k]["status"] in ("proposed", "pending")]
                    victim = min(waiting, key=lambda d: (d["value"], d["id"]), default=None)
                    if victim is None or victim["value"] >= val:
                        continue
                    self._supersede(victim, key, "outranked")
                    out.append((victim, False))
                else:
                    self.admitted[bucket] = self.admitted.get(bucket, 0) + 1
                self.admitted_groups.add(f.group)
            elif cur is not None:
                old = self.docs[cur]
                self._supersede(old, key, cause or "updated")
                out.append((old, False))
            doc = self.docs.get(key)
            if doc is None:
                doc = self.docs[key] = {"id": f"insight-{self.match.match_id}-{key}", "type": "insight", "match_id": self.match.match_id, "key": key, "group": f.group,
                                        "status": "proposed", "version": 1}
                self._fill(doc, f, r, val)
                self._mark(doc, "proposed")
            else:  # the level comes back (a correction undid what superseded it)
                self._refresh(doc)
                doc["version"] += 1
                self._fill(doc, f, r, val)
                doc["status"] = "proposed"
                doc.pop("superseded_by", None)
                doc.pop("supersede_cause", None)
                self._mark(doc, "revived")
            self.current[f.group] = key
            self.bucket_of.setdefault(key, bucket if cur is None else self.bucket_of.get(cur, bucket))
            out.append((doc, True))
        for group, key in list(self.current.items()):
            if group not in groups:
                doc = self.docs[key]
                self._supersede(doc, None, cause or "no longer true")
                out.append((doc, False))
        return out

    def _stat_of_the_match(self, m: dict) -> list[tuple[dict, bool]]:
        ranked = [x for x in self.ranked if x[0] >= self.min_value] or self.ranked[:1]
        if not ranked:
            return []
        val, top_bits, top, r = ranked[0]
        facts, meanings = self._headline(top, r)
        facts |= {"top_fact": top.key, "top_value": val}
        meanings |= {"top_fact": "key of the fact that ranks first", "top_value": "its value on the desk, 0 to 100 (75 % rarity, 25 % weight in the result)"}
        why = f"{top.key} ranks first with a value of {val} and {top_bits:.1f} bits of surprisal"
        if len(ranked) > 1:
            v2, b2, second, _ = ranked[1]
            ratio = 2 ** (top_bits - b2)
            ratio = int(round(ratio)) if ratio >= 10 else round(ratio, 1)  # "230 times rarer" reads better than 227.5
            facts |= {"runner_up": second.key, "runner_up_value": v2, "runner_up_surprisal_bits": round(b2, 1), "times_rarer_than_runner_up": ratio}
            meanings |= {"runner_up": "key of the fact that ranks second", "runner_up_value": "its value, 0 to 100", "runner_up_surprisal_bits": "its rarity in bits",
                         "times_rarer_than_runner_up": "how many times rarer the top fact is than the second, 2 to the power of the difference in bits"}
            why += f", against {b2:.1f} bits for {second.key} ({v2}): about {ratio} times rarer" if ratio >= 1 else f"; {second.key} is rarer ({b2:.1f} bits) but weighs less in the result"
        key = "stat-of-the-match"
        doc = self.docs.get(key)
        content = {"kind": top.kind, "minute_label": m.get("minute_label"), "clock_s": m.get("clock_s"), "event_id": top.event_id, "headline_facts": facts, "field_meanings": meanings,
                   "rarity": {"p": r["p"], "surprisal_bits": r["surprisal_bits"], "basis": r["basis"]}, "last_time": r["last"], "value": val, "evidence": list(top.evidence),
                   "why_first": why, "ranked": [{"key": f.key, "kind": f.kind, "value": v, "surprisal_bits": round(b, 2), "basis": rr["basis"]} for v, b, f, rr in ranked[:5]]}
        if doc is None:
            doc = self.docs[key] = {"id": f"insight-{self.match.match_id}-{key}", "type": "insight", "match_id": self.match.match_id, "key": key, "group": key,
                                    "stat_of_the_match": True, "status": "proposed", "version": 1, "under_review": False, **content}
            self._mark(doc, "proposed")
            return [(doc, True)]
        if all(doc.get(k) == v for k, v in content.items()):
            return []
        self._refresh(doc)
        doc["version"] += 1
        doc.update(content)
        if doc["status"] in ACTIVE:
            doc["status"] = "proposed"
        self._mark(doc, "updated")
        return [(doc, True)]

    def active(self) -> list[dict]:
        return [self.docs[k] for k in self.current.values()]


# --- the catalogue ----------------------------------------------------------------------------------

def scorer_milestones(e: InsightEngine, ctx: dict) -> list[Fact]:
    """Brace, hat-trick, four...: one group per player, the level is his goal count. A substitute's goals are compared with substitutes only."""
    out = []
    by_player: dict[tuple[str, str], list[LiveGoal]] = {}
    for g in ctx["goals"]:
        by_player.setdefault((g.side, g.player["id"]), []).append(g)
    for (side, pid), goals in by_player.items():
        n = len(goals)
        if n < 2:
            continue
        sub = e.sub_of(side, pid, goals[0].seq)
        last = goals[-1]
        if sub:
            q = Query(f"sub-goals-{n}", lambda f, n=n: (int(any(len(ms) >= n and min(ms) >= f.sub_in[p] for p, ms in f.player_goals().items() if p in f.sub_in)), 1),
                      f"{n} or more goals by a substitute", "matches",
                      pred=lambda f, n=n: any(len(ms) >= n and min(ms) >= f.sub_in[p] for p, ms in f.player_goals().items() if p in f.sub_in),
                      who=lambda f, n=n: next((p for p, ms in f.player_goals().items() if len(ms) >= n and p in f.sub_in and min(ms) >= f.sub_in[p]), None))
        else:
            q = Query(f"player-goals-{n}", lambda f, n=n: (int(any(len(ms) >= n for ms in f.player_goals().values())), 1), f"{n} or more goals by one player", "matches",
                      pred=lambda f, n=n: any(len(ms) >= n for ms in f.player_goals().values()),
                      who=lambda f, n=n: next((p for p, ms in f.player_goals().items() if len(ms) >= n), None))
        facts = {"player": last.player["name"], "team": ctx["teams"][side], "goals": n, "goal_minutes": ", ".join(g.minute_label for g in goals), "substitute": bool(sub)}
        meanings = {"player": "player who scored", "team": "his team", "goals": "goals he has scored in this match that count (goals ruled out are not counted)",
                    "goal_minutes": "minutes of those goals", "substitute": "true if he came off the bench before his first goal"}
        if sub:
            facts["came_on"] = sub[2]
            meanings["came_on"] = "minute he came on"
        out.append(Fact(f"scorer-{pid}", str(n), "milestone", facts, meanings, e.card_value(last.event_id), last.event_id, last.minute_label, last.clock_s,
                        [g.event_id for g in goals], q))
    return out


def fastest_hat_trick(e: InsightEngine, ctx: dict) -> list[Fact]:
    """Minutes between a player's first and third goal, against every hat-trick in the history."""
    out = []
    by_player: dict[tuple[str, str], list[LiveGoal]] = {}
    for g in ctx["goals"]:
        by_player.setdefault((g.side, g.player["id"]), []).append(g)
    for (side, pid), goals in by_player.items():
        if len(goals) < 3:
            continue
        span = round((goals[2].clock_s - goals[0].clock_s) / 60)

        def fn(f, span=span):
            hts = [ms for ms in f.player_goals().values() if len(ms) >= 3]
            return sum(1 for ms in hts if ms[2] - ms[0] <= span), len(hts)

        q = Query(f"hat-trick-within-{span}", fn, f"hat-tricks completed within {span} minutes", "hat-tricks",
                  pred=lambda f, span=span: any(len(ms) >= 3 and ms[2] - ms[0] <= span for ms in f.player_goals().values()),
                  who=lambda f, span=span: next((p for p, ms in f.player_goals().items() if len(ms) >= 3 and ms[2] - ms[0] <= span), None))
        third = goals[2]
        out.append(Fact(f"hat-trick-speed-{pid}", str(span), "record",
                        {"player": third.player["name"], "team": ctx["teams"][side], "hat_trick_minutes": span, "first_goal": goals[0].minute_label, "third_goal": third.minute_label},
                        {"player": "player who scored the hat-trick", "team": "his team", "hat_trick_minutes": "minutes between his first and his third goal",
                         "first_goal": "minute of his first goal", "third_goal": "minute of his third goal"},
                        0.3, third.event_id, third.minute_label, third.clock_s, [g.event_id for g in goals[:3]], q))
    return out


def comebacks(e: InsightEngine, ctx: dict) -> list[Fact]:
    """A team that trailed by two or more and came back level, ahead, or to win: against every team that trailed by as much."""
    out = []
    goals = ctx["goals"]
    order = [g.side for g in goals]
    for side in ("home", "away"):
        worst, stage = comeback_stage(order, side, final=e.final)
        if worst < 2 or stage == 0:
            continue
        diff, worst_at, stage_goal = 0, -1, None
        for i, s in enumerate(order):
            diff += 1 if s == side else -1
            if -diff == worst and worst_at < 0:
                worst_at = i
        diff = sum(1 if s == side else -1 for s in order[:worst_at + 1])
        for g in goals[worst_at + 1:]:
            diff += 1 if g.side == side else -1
            if (stage >= 2 and diff > 0) or (stage == 1 and diff >= 0):
                stage_goal = g
                break
        trail = {"home": sum(1 for s in order[:worst_at + 1] if s == "home"), "away": sum(1 for s in order[:worst_at + 1] if s == "away")}

        def fn(f, worst=worst, stage=stage):
            k = n = 0
            for s in ("home", "away"):
                w, st = f.comeback(s)
                if w >= worst:
                    n += 1
                    k += st >= stage
            return k, n

        what = {1: f"teams {worst} or more goals down that drew level", 2: f"teams {worst} or more goals down that went ahead", 3: f"teams {worst} or more goals down that won"}[stage]
        q = Query(f"comeback-{worst}-{stage}", fn, what, f"teams that trailed by {worst} or more", pred=lambda f, worst=worst, stage=stage: fn(f, worst, stage)[0] > 0)
        club = ctx["teams"][side]
        at = goals.index(stage_goal) + 1 if stage < 3 and stage_goal else len(goals)
        reached = {s: sum(1 for g in goals[:at] if g.side == s) for s in ("home", "away")}
        facts = {"team": club, "opponent": ctx["teams"][other(side)], "deficit_goals": worst, "trailing_score": f"{trail['home']}-{trail['away']}",
                 "stage": STAGES[stage], "score": f"{reached['home']}-{reached['away']}"}
        meanings = {"team": "team that came back", "opponent": "the other team", "deficit_goals": "largest number of goals the team trailed by",
                    "trailing_score": "score (home-away) when the team trailed by the most", "stage": "how far the comeback went: level, ahead, or won at full time",
                    "score": "score (home-away) when the comeback reached that stage; the final score once won"}
        if club in e.view.clubs:
            hit = e.last(f"club-comeback-{club}-{worst}-{stage}", lambda f, club=club, worst=worst, stage=stage: any(
                f.club(s) == club and f.comeback(s)[0] >= worst and f.comeback(s)[1] >= stage for s in ("home", "away")))
            if hit is None:
                facts["club_first_time"] = True
                meanings["club_first_time"] = f"true if {club} have never done it in the league history"
            else:
                facts |= {"club_last_season": hit.season, "club_seasons_since": e.view.current_idx - hit.season_idx}
                meanings |= {"club_last_season": f"last season {club} did it", "club_seasons_since": f"seasons since {club} last did it"}
        rel = 1.0 if stage == 3 else e.card_value(stage_goal.event_id if stage_goal else None)
        moment = stage_goal or (goals[-1] if goals else None)
        tied = moment if stage < 3 else None  # a win is a full-time fact
        out.append(Fact(f"comeback-{side}", f"{worst}-{stage}", "rarity", facts, meanings, rel, tied.event_id if tied else None,
                        tied.minute_label if tied else None, tied.clock_s if tied else None, [g.event_id for g in goals[:at]], q))
    return out


def half_time_lead_lost(e: InsightEngine, ctx: dict) -> list[Fact]:
    """A team that led at half-time, now behind (or beaten at full time): against every half-time lead, and the club's own last time."""
    if not e.half_time:
        return []
    goals = ctx["goals"]
    ht = {"home": sum(1 for g in goals if g.period == 1 and g.side == "home"), "away": sum(1 for g in goals if g.period == 1 and g.side == "away")}
    if ht["home"] == ht["away"]:
        return []
    side = "home" if ht["home"] > ht["away"] else "away"
    lead = ht[side] - ht[other(side)]
    score = e.match.score()
    run = dict(ht)
    behind = None  # the goal that put them behind: what happened stays true if they equalise later
    for g in goals:
        if g.period < 2:
            continue
        run[g.side] += 1
        if run[side] < run[other(side)]:
            behind = g
            break
    if behind is None:
        return []
    stage = 2 if e.final and score[side] < score[other(side)] else 1
    club, venue = ctx["teams"][side], side
    shown = score if stage == 2 else run

    def fn(f, lead=lead, stage=stage):
        if f.ht is None:
            return 0, 0
        k = n = 0
        for s, i in (("home", 0), ("away", 1)):
            if f.ht[i] - f.ht[1 - i] >= lead:
                n += 1
                k += f.result(s) == "L" if stage == 2 else _trailed_after_ht(f, s)
        return k, n

    what = f"teams leading by {lead} or more at half-time that " + ("lost" if stage == 2 else "fell behind")
    q = Query(f"ht-lead-{lead}-{stage}", fn, what, f"half-time leads of {lead} or more", pred=lambda f: fn(f)[0] > 0)
    facts = {"team": club, "opponent": ctx["teams"][other(side)], "half_time_score": f"{ht['home']}-{ht['away']}", "score": f"{shown['home']}-{shown['away']}",
             "venue": venue, "stage": "lost" if stage == 2 else "behind"}
    meanings = {"team": "team that led at half-time", "opponent": "the other team", "half_time_score": "score at half-time, home-away", "score": "score (home-away) when the team fell behind; the final score once lost",
                "venue": "whether the team plays at home or away", "stage": "behind: trailing now; lost: beaten at full time"}
    if club in e.view.clubs:
        i = 0 if venue == "home" else 1
        hit = e.last(f"club-ht-lead-lost-{club}-{venue}", lambda f: f.club(venue) == club and f.ht is not None and f.ht[i] > f.ht[1 - i] and f.result(venue) == "L")
        if hit is None:
            facts["club_first_time"] = True
            meanings["club_first_time"] = f"true if {club} have never lost a {venue} match they led at half-time in the league history"
        else:
            facts |= {"club_last_season": hit.season, "club_last_match": f"{hit.home} {hit.hg}-{hit.ag} {hit.away}", "club_seasons_since": e.view.current_idx - hit.season_idx}
            meanings |= {"club_last_season": f"last season {club} lost a {venue} match they led at half-time",
                         "club_last_match": "that match, with its final score", "club_seasons_since": f"seasons since {club} last lost a {venue} match they led at half-time"}
    rel = 1.0 if stage == 2 else e.card_value(behind.event_id if behind else None)
    tied = behind if stage == 1 else None
    return [Fact(f"ht-lead-lost-{side}", str(stage), "streak", facts, meanings, rel, tied.event_id if tied else None,
                 tied.minute_label if tied else None, tied.clock_s if tied else None, [g.event_id for g in goals[:goals.index(behind) + 1]] if stage == 1 else [g.event_id for g in goals], q)]


def _trailed_after_ht(f: Features, side: str) -> int:
    if f.ht is None or not f.goals:
        return 0
    diff = (f.ht[0] - f.ht[1]) if side == "home" else (f.ht[1] - f.ht[0])
    for g in f.goals:
        if g.minute <= 45:
            continue
        diff += 1 if g.side == side else -1
        if diff < 0:
            return 1
    return 0


def late_goals(e: InsightEngine, ctx: dict) -> list[Fact]:
    """An equaliser or a go-ahead goal from the 85th minute: against every match in the history."""
    out = []
    score = {"home": 0, "away": 0}
    for g in ctx["goals"]:
        before = score[g.side] - score[other(g.side)]
        score[g.side] += 1
        if g.period < 2 or g.minute < LATE_MINUTE or before not in (-1, 0):
            continue
        kind = "equaliser" if before == -1 else "go-ahead goal"

        def fn(f, before=before):
            k = 0
            s = {"home": 0, "away": 0}
            for h in f.goals:
                d = s[h.side] - s["away" if h.side == "home" else "home"]
                s[h.side] += 1
                if h.minute >= LATE_MINUTE and d == before:
                    k += 1
            return min(k, 1), 1

        q = Query(f"late-{kind}", fn, f"matches with a {kind} from the {LATE_MINUTE}th minute", "matches", pred=lambda f, fn=fn: fn(f)[0] > 0,
                  who=lambda f, before=before: _late_scorer(f, before))
        out.append(Fact(f"late-{g.event_id}", kind.split()[0], "rarity",
                        {"player": g.player["name"], "team": ctx["teams"][g.side], "minute": g.minute_label, "goal_type": kind, "score_after": f"{score['home']}-{score['away']}"},
                        {"player": "scorer", "team": "his team", "minute": "minute of the goal", "goal_type": "equaliser or go-ahead goal", "score_after": "score after the goal, home-away"},
                        e.card_value(g.event_id), g.event_id, g.minute_label, g.clock_s, [g.event_id], q))
    return out


def _late_scorer(f: Features, before: int) -> str | None:
    s = {"home": 0, "away": 0}
    for h in f.goals:
        d = s[h.side] - s["away" if h.side == "home" else "home"]
        s[h.side] += 1
        if h.minute >= LATE_MINUTE and d == before:
            return h.player
    return None


def red_card_effects(e: InsightEngine, ctx: dict) -> list[Fact]:
    """A team down to ten that scores, or wins: against every team that had a player sent off."""
    out = []
    reds = sorted(e.match.red_cards.values())
    score = e.match.score()
    for side in ("home", "away"):
        mine = [seq for seq, s in reds if s == side]
        if not mine:
            continue
        scored = [g for g in ctx["goals"] if g.side == side and g.seq > mine[0]]
        won = e.final and score[side] > score[other(side)]
        if not scored and not won:
            continue
        stage = "won" if won else "scored"

        def fn(f, stage=stage):
            k = n = 0
            for s in ("home", "away"):
                red = [mn for rs, mn in f.reds if rs == s]
                if not red:
                    continue
                n += 1
                k += f.result(s) == "W" if stage == "won" else any(g.side == s and g.minute > red[0] for g in f.goals)
            return k, n

        q = Query(f"ten-men-{stage}", fn, f"teams with a player sent off that {'won' if won else 'scored after the red card'}", "teams with a player sent off",
                  pred=lambda f, fn=fn: fn(f)[0] > 0)
        last = scored[-1] if scored else None
        out.append(Fact(f"ten-men-{side}", stage, "rarity",
                        {"team": ctx["teams"][side], "goals_after_red": len(scored), "score": f"{score['home']}-{score['away']}", "stage": stage},
                        {"team": "team reduced to ten", "goals_after_red": "goals it scored after the red card", "score": "current score, home-away",
                         "stage": "scored: scored with ten men; won: won the match with ten men"},
                        1.0 if won else e.card_value(last.event_id if last else None), last.event_id if last and not won else None,
                        last.minute_label if last else None, last.clock_s if last else None, [g.event_id for g in scored], q))
    return out


def runs(e: InsightEngine, ctx: dict) -> list[Fact]:
    """At full time: an unbeaten or winless run, from the end of the history, ended or extended by this result."""
    if not e.final:
        return []
    out = []
    score = e.match.score()
    current = e.view.runs()["current"]
    for side in ("home", "away"):
        club = ctx["teams"][side]
        if club not in current:
            continue
        result = "W" if score[side] > score[other(side)] else "D" if score[side] == score[other(side)] else "L"
        for kind, breaks in (("unbeaten", "L"), ("winless", "W")):
            n = current[club][kind]
            if result == breaks and n >= 5:
                stage, length, rel = "ended", n, 1.0
            elif result != breaks and n + 1 >= 8:
                stage, length, rel = "extended", n + 1, 0.6
            else:
                continue
            q = Query(f"run-{kind}-{length}", None, f"{kind} runs of {length} or more matches", f"{kind} runs", counts=e.view.run_tail(kind, length))
            out.append(Fact(f"run-{kind}-{side}", f"{stage}-{length}", "streak",
                            {"team": club, "run": kind, "run_matches": length, "stage": stage, "result": result},
                            {"team": "team whose run it is", "run": "unbeaten (no defeat) or winless (no win)", "run_matches": "length of the run in league matches, this match included if it extends it",
                             "stage": "ended by this result, or extended", "result": "this match: W win, D draw, L defeat"},
                            rel, None, None, None, [], q))
    return out


def head_to_head(e: InsightEngine, ctx: dict) -> list[Fact]:
    """At full time: the winner's first win in this fixture for N seasons, and its biggest win over this opponent."""
    if not e.final:
        return []
    teams, score = ctx["teams"], e.match.score()
    if score["home"] == score["away"] or teams["home"] not in e.view.clubs or teams["away"] not in e.view.clubs:
        return []
    side = "home" if score["home"] > score["away"] else "away"
    winner, loser = teams[side], teams[other(side)]
    meetings = e.view.head_to_head(teams["home"], teams["away"])
    out = []
    here = [m for m in meetings if m.get("home") == teams["home"] and m.get("away") == teams["away"]]
    wins_here = [m for m in here if (m["home_goals"] > m["away_goals"]) == (side == "home") and m["home_goals"] != m["away_goals"]]
    idx = e.view.season_idx
    if here:
        if wins_here:
            gap = e.view.current_idx - idx.get(wins_here[0]["season"], 0)
        else:
            gap = e.view.current_idx - idx.get(here[-1]["season"], 0)
        if gap >= 3:
            q = Query(f"drought-{side}-{gap}", None, f"{side} wins in a fixture after {gap} or more seasons without one", f"{side} wins", counts=e.view.drought_tail(side, gap))
            facts = {"team": winner, "opponent": loser, "venue": side, "seasons_without_win": gap, "meetings_here": len(here), "first_win_here": not wins_here}
            meanings = {"team": "winner", "opponent": "loser", "venue": "where the winner played: home or away", "seasons_without_win": "seasons since the winner last won this fixture at this ground (or since the first meeting)",
                        "meetings_here": "past league meetings with the same home and away clubs", "first_win_here": "true if the winner had never won this fixture at this ground"}
            if wins_here:
                facts["last_win_season"] = wins_here[0]["season"]
                meanings["last_win_season"] = "season of the winner's previous win in this fixture at this ground"
            out.append(Fact(f"h2h-drought-{side}", str(gap), "streak", facts, meanings, 0.8, None, None, None, [], q))
    margin = abs(score["home"] - score["away"])
    margins = [(m["home_goals"] - m["away_goals"]) * (1 if m["home"] == winner else -1) for m in meetings]
    best = max(margins, default=0)
    if margin >= 2 and margin >= best:
        q = Query(f"margin-{margin}", lambda f, margin=margin: (int(abs(f.hg - f.ag) >= margin), 1), f"wins by {margin} or more goals", "matches",
                  pred=lambda f, margin=margin: abs(f.hg - f.ag) >= margin)
        facts = {"team": winner, "opponent": loser, "margin": margin, "score": f"{score['home']}-{score['away']}", "meetings": len(meetings), "record": "new" if margin > best else "equalled"}
        meanings = {"team": "winner", "opponent": "loser", "margin": "winning margin in goals", "score": "final score, home-away", "meetings": "past league meetings of the two clubs",
                    "record": f"new: {winner}'s biggest win over {loser} in the history; equalled: as big as their biggest"}
        if margin == best:
            prev = next(m for m, mg in zip(meetings, margins) if mg == best)
            facts["equalled_season"] = prev["season"]
            meanings["equalled_season"] = "season of the most recent win by as many goals"
        out.append(Fact(f"h2h-margin-{side}", str(margin), "record", facts, meanings, 0.8, None, None, None, [], q))
    return out


def team_records(e: InsightEngine, ctx: dict) -> list[Fact]:
    """Goals in the match against the history: a team's most goals in a match, and matches with as many goals."""
    out = []
    score = e.match.score()
    total = score["home"] + score["away"]
    goals = ctx["goals"]
    if total >= 6:
        q = Query(f"total-goals-{total}", lambda f, t=total: (int(f.hg + f.ag >= t), 1), f"matches with {total} or more goals", "matches", pred=lambda f, t=total: f.hg + f.ag >= t)
        last = goals[-1]
        out.append(Fact("goals-total", str(total), "record", {"goals": total, "score": f"{score['home']}-{score['away']}", "minute": last.minute_label},
                        {"goals": "goals that count in this match, both teams", "score": "current score, home-away", "minute": "minute of the latest goal"},
                        0.2, last.event_id, last.minute_label, last.clock_s, [g.event_id for g in goals], q))
    for side in ("home", "away"):
        club, g = ctx["teams"][side], score[side]
        if g < 4 or club not in e.view.clubs:
            continue
        best = e.view.club_max_goals(club)
        if g < best:
            continue
        q = Query(f"team-goals-{g}", lambda f, g=g: (int(f.hg >= g) + int(f.ag >= g), 2), f"teams scoring {g} or more in a match", "team matches",
                  pred=lambda f, g=g: f.hg >= g or f.ag >= g)
        mine = [x for x in goals if x.side == side]
        out.append(Fact(f"club-goals-{side}", str(g), "record",
                        {"team": club, "goals": g, "club_record": best, "record": "new" if g > best else "equalled"},
                        {"team": "team", "goals": "its goals in this match that count", "club_record": "its most goals in a league match in the history before today",
                         "record": "new: more than ever before; equalled: as many as its record"},
                        0.4, mine[-1].event_id, mine[-1].minute_label, mine[-1].clock_s, [x.event_id for x in mine], q))
    return out


def match_kpis(e: InsightEngine, ctx: dict) -> list[Fact]:
    """Against the match itself, at half-time and full time: goals against xG (Poisson), shots against the pre-match share (binomial), possession swing between halves."""
    out = []
    teams = ctx["teams"]
    xg_home, xg_away = e.match.model_inputs()
    share_home = xg_home / (xg_home + xg_away)
    windows = []
    if e.half_time:
        windows.append((1, "first half"))
    if e.final:
        windows.append((None, "match"))
    voided = e.match.voided
    for period, label in windows:
        shots = {"home": 0, "away": 0}
        xg = {"home": 0.0, "away": 0.0}
        for a in e.actions(period):
            if a["type"] == "action" and a["action_type"].startswith("shot") and "shot" in a and a["event_id"] not in voided:
                shots[a["team"]] += 1
                xg[a["team"]] += a["shot"]["xg"]
        goals = {s: sum(1 for g in ctx["goals"] if g.side == s and (period is None or g.period == period)) for s in ("home", "away")}
        tag = "p1" if period == 1 else "ft"
        for side in ("home", "away"):
            g, x = goals[side], xg[side]
            if g >= 3 and g - x >= 1.5:
                p = poisson_sf(g, x)
                out.append(Fact(f"xg-{tag}-{side}", str(g), "tactical",
                                {"team": teams[side], "goals": g, "xg": round(x, 1), "window": label},
                                {"team": "team", "goals": f"its goals in the {label} that count", "xg": f"expected goals of its shots in the {label}", "window": "period measured"},
                                0.3, None, None, None, [x_.event_id for x_ in ctx["goals"] if x_.side == side], model_p=p,
                                model_basis=f"{g} or more goals from {x:.1f} xG: Poisson probability {p:.4f} ({teams[side]}, {label})"))
            n, s = shots["home"] + shots["away"], shots[side]
            q = share_home if side == "home" else 1 - share_home
            if n >= 8 and s / n >= 0.75:
                p = binom_sf(s, n, q)
                out.append(Fact(f"shots-{tag}-{side}", f"{s}-{n - s}", "tactical",
                                {"team": teams[side], "shots": s, "opponent_shots": n - s, "expected_share_pct": round(100 * q), "window": label},
                                {"team": "team", "shots": f"its shots in the {label}", "opponent_shots": f"the other team's shots in the {label}",
                                 "expected_share_pct": "share of the shots it would take on its pre-match expected goals, in %", "window": "period measured"},
                                0.2, None, None, None, [], model_p=p,
                                model_basis=f"{s} of {n} shots against an expected share of {q:.0%}: binomial probability {p:.4f} ({label})"))
    if e.final:
        share, possessions = {}, {}
        for period in (1, 2):
            passes = {"home": 0, "away": 0}
            seqs = set()
            for a in e.actions(period):
                if a["type"] == "action" and a["action_type"] == "pass":
                    passes[a["team"]] += 1
                    seqs.add(a.get("possession"))
            total = passes["home"] + passes["away"]
            if total:
                share[period] = passes["home"] / total
                possessions[period] = max(len(seqs), 1)
        if len(share) == 2:
            delta = share[2] - share[1]
            if abs(delta) >= 0.12:
                side = "home" if delta > 0 else "away"
                sd = (0.25 * (1 / possessions[1] + 1 / possessions[2])) ** 0.5
                p = 2 * normal_sf(abs(delta) / sd)
                first = round(100 * (share[1] if side == "home" else 1 - share[1]))
                second = round(100 * (share[2] if side == "home" else 1 - share[2]))
                out.append(Fact(f"possession-swing", f"{side}-{second}", "tactical",
                                {"team": teams[side], "first_half_pass_share_pct": first, "second_half_pass_share_pct": second},
                                {"team": "team whose share of the passes rose", "first_half_pass_share_pct": "its share of the passes in the first half, in %",
                                 "second_half_pass_share_pct": "its share of the passes in the second half, in %"},
                                0.2, None, None, None, [], model_p=p,
                                model_basis=f"pass share {first}% then {second}%: two-sided normal probability {p:.4f}, one possession counted as one trial"))
    return out


DETECTORS = (scorer_milestones, fastest_hat_trick, comebacks, half_time_lead_lost, late_goals, red_card_effects, runs, head_to_head, team_records, match_kpis)
