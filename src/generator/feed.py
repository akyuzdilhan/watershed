"""The feed contract and the hidden ground truth.

Every message carries the same envelope: schema_version, generator_version, match_id, message_id, seq (provider order, contiguous), type, wall_s (broadcast clock) and emitted_at_s (when the provider sends it), then the match clock (period, clock_s, minute, minute_label) except in match_info. Message types:

- match_info: teams, line-ups, pre-match expected goals, referee, pitch frame
- period_start, period_end, added_time, full_time
- action: one SPADL-style action (action_type, result, bodypart, start and end in metres seen from the acting team), its event_id and version, the running score, and a 360-style freeze_frame
- substitution, stoppage
- var_check: started, then completed with decision "stands" or "overturned"
- correction: a typed operation on an earlier action (void, reassign or update), with target_version, new_version, cause and the running score

The stream then gets what a real feed suffers: latency, a few late (out-of-order) messages and a few duplicates. The ground truth says what happened, what each correction means for a card already on air, and which messages were late or duplicated, so the pipeline can be scored against it.
"""

from generator.calibration import count

SCHEMA_VERSION = "2.0"
GENERATOR_VERSION = "2.0.0"
ID_TAG = "v" + GENERATOR_VERSION.split(".")[0]  # in every id, so a new generator never reuses an old id


def build_stream(engine) -> tuple[list[dict], dict]:
    """Envelope, sequence numbers and network incidents; returns the messages in arrival order."""
    rng = engine.rng
    stream, late, duplicates = [], [], []
    for seq, m in enumerate(engine.messages, start=1):
        latency = float(rng.uniform(0.4, 1.5))
        is_action = m["type"] == "action"
        if is_action and rng.random() < 0.02:
            latency += float(rng.uniform(4, 10))
            late.append(m["message_id"])
        msg = {
            "schema_version": SCHEMA_VERSION,
            "generator_version": GENERATOR_VERSION,
            "match_id": engine.match_id,
            "message_id": m["message_id"],
            "seq": seq,
            "type": m["type"],
            "wall_s": m["wall_s"],
            "emitted_at_s": round(m["wall_s"] + latency, 2),
        }
        msg.update({k: v for k, v in m.items() if k not in msg})
        stream.append(msg)
        if is_action and rng.random() < 0.01:
            duplicates.append(m["message_id"])
            stream.append({**msg, "emitted_at_s": round(msg["emitted_at_s"] + float(rng.uniform(1, 3)), 2)})
    stream.sort(key=lambda m: (m["emitted_at_s"], m["seq"]))
    return stream, {"late": late, "duplicates": duplicates}


class TruthRecorder:
    """What really happened, recorded by the engine as it plays."""

    def __init__(self, engine) -> None:
        self.e = engine
        self.moments: dict[str, dict] = {}
        self.shots: list[dict] = []
        self.goals: list[dict] = []
        self.var_checks: list[dict] = []
        self.offsides: list[dict] = []
        self.cards: list[dict] = []
        self.corrections: list[dict] = []

    @staticmethod
    def _where(ev: dict) -> dict:
        return {"event_id": ev.get("event_id", ev["message_id"]), "period": ev["period"], "minute_label": ev["minute_label"], "clock_s": ev["clock_s"], "wall_s": ev["wall_s"]}

    def moment(self, m, ev: dict) -> None:
        """A planted moment and the action (or, for a substitution, the message) that carries it."""
        player = ev.get("player", ev.get("player_in"))
        self.moments[m.key] = {"key": m.key, "kind": m.kind, "team": m.side, "player": player, **self._where(ev), "score_at": dict(self.e.state.score)}

    def shot(self, ev: dict, moment, score_before: dict, reds_before: dict) -> None:
        self.shots.append({
            **self._where(ev), "team": ev["team"], "player_id": ev["player"]["id"], "action_type": ev["action_type"],
            "xg": ev["shot"]["xg"], "outcome": ev["shot"]["outcome"], "score_before": score_before, "reds_before": reds_before,
            "planted": moment.key if moment else None,
        })

    def goal(self, ev: dict, moment) -> None:
        self.goals.append({**self._where(ev), "team": ev["team"], "player_id": ev["player"]["id"], "planted": moment.key if moment else None})

    def var_check(self, started: dict, done: dict) -> None:
        self.var_checks.append({
            "check_id": done["check_id"], "target_event_id": done["target_event_id"], "reason": done["reason"], "decision": done["decision"],
            "started_wall_s": started["wall_s"], "completed_wall_s": done["wall_s"], "completed_minute_label": done["minute_label"],
        })

    def offside(self, ev: dict, runner, margin: float, flagged: bool) -> None:
        self.offsides.append({"event_id": ev["event_id"], "player_id": runner.id, "margin_m": round(margin, 2), "flagged": flagged, "minute_label": ev["minute_label"]})

    def card(self, ev: dict, card: str) -> None:
        self.cards.append({**self._where(ev), "team": ev["team"], "player_id": ev["player"]["id"], "card": card})

    def correction(self, msg: dict) -> None:
        self.corrections.append({
            "message_id": msg["message_id"], "op": msg["op"], "target_event_id": msg["target_event_id"], "cause": msg["cause"],
            "wall_s": msg["wall_s"], "minute_label": msg["minute_label"], "related_message_id": msg.get("related_message_id"), "score": msg["score"],
        })

    def build(self, stream: list[dict], incidents: dict) -> dict:
        goal_ids = {g["event_id"] for g in self.goals}
        events = {m["event_id"]: m for m in self.e.messages if m["type"] == "action"}
        goals = []
        for g in self.goals:
            ops = [c for c in self.corrections if c["target_event_id"] == g["event_id"]]
            void = next((c for c in ops if c["op"] == "void"), None)
            related = {c["related_message_id"] for c in ops if c["related_message_id"]}
            affected = sorted({g["event_id"]} | {c["target_event_id"] for c in self.corrections if c["related_message_id"] in related})
            action = "retract" if void else "rewrite" if any(c["op"] == "reassign" for c in ops) else "none"
            score = void["score"] if void else events[g["event_id"]]["score"]
            goals.append({**g, "counts": void is None, "expected_card_action": action, "affected_event_ids": affected, "score_after": f"{score['home']}-{score['away']}"})
        by_event = {g["event_id"]: g for g in goals}
        moments = []
        for mo in self.moments.values():
            g = by_event.get(mo["event_id"])
            if g is not None:
                moments.append({**mo, "expected_card_action": g["expected_card_action"], "affected_event_ids": g["affected_event_ids"], "score_after": g["score_after"]})
            else:
                score = mo["score_at"]
                moments.append({**mo, "expected_card_action": "none", "affected_event_ids": [mo["event_id"]], "score_after": f"{score['home']}-{score['away']}"})
        assisted = {a for g in goals for a in g["affected_event_ids"]}
        corrections = [{**c, "card_relevant": c["target_event_id"] in goal_ids or c["target_event_id"] in assisted} for c in self.corrections]
        s = self.e.state
        teams = {}
        for side, t in s.teams.items():
            teams[side] = {"name": t.name, "formation": t.formation, "expected_goals": round(t.expected_goals, 3), "squad": [{**p.ref(), "role": p.role} for p in t.squad]}
        return {
            "schema_version": SCHEMA_VERSION,
            "generator_version": GENERATOR_VERSION,
            "match_id": self.e.match_id,
            "seed": self.e.seed,
            "story": self.e.story,
            "teams": teams,
            "final_score": dict(s.score),
            "moments": moments,
            "goals": goals,
            "shots": self.shots,
            "var_checks": self.var_checks,
            "offsides": self.offsides,
            "cards": self.cards,
            "corrections": corrections,
            "incidents": incidents,
            "counts": count(stream),
        }
