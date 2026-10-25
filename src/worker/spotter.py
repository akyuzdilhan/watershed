"""The Spotter: which moments deserve a card, and what a correction means for a card already proposed or on air.

Deterministic, so every card can be traced back to numbers. A moment's value is how far it moves the result probabilities (0 to 1):
- a goal: the move it caused;
- a shot that did not go in (saved, blocked, wide, off the woodwork): its xG times the move it would have caused, so that a late half-chance does not outweigh a real chance;
- a red card: the move of a team going down to ten.

Every goal and every red card gets a card; another chance needs a value of at least `threshold`. A VAR check puts the card under review; a void retracts it; a reassignment rewrites it.
"""

from dataclasses import dataclass, field

from winprob.model import WinProbModel
from worker.match import Match, swing, team_win

SHOT_PREFIX = "shot"


def pct(p: float) -> int:
    return round(100 * p)


def shown_pct(p: float) -> int:
    """A result probability on a card: 1 to 99, never 0% or 100% before the final whistle (same bounds as `writer.shown`)."""
    return min(max(pct(p), 1), 99)


@dataclass
class Spotter:
    match: Match
    model: WinProbModel
    threshold: float = 0.08
    cards: dict[str, dict] = field(default_factory=dict)  # event id -> card
    saves: dict[int, tuple] = field(default_factory=dict)  # sequence number of the keeper's action -> (shot, value, before, if goal)

    def process(self, m: dict) -> list[dict]:
        """What message `m` changes for the desk: new cards, and actions on existing ones."""
        if m["type"] == "action":
            if m["action_type"] == "keeper_save" and m["seq"] in self.saves:
                shot, value, before, if_goal = self.saves.pop(m["seq"])
                return [self._card(m, "save", value, before, if_goal, "if_goal", shot=shot)]
            card = self._moment(self.match.versions.get(m["event_id"], m))  # as corrected, if a correction came first
            return [card] if card else []
        if m["type"] == "var_check" and m["target_event_id"] in self.cards:
            card = self.cards[m["target_event_id"]]
            if m["status"] == "started":
                card["under_review"] = True
                return [self._action(card, "hold", m, f"VAR check: {m['reason']}")]
            card["under_review"] = False
            return [self._action(card, "release", m, "VAR: goal stands")] if m["decision"] == "stands" else []
        if m["type"] == "correction" and m["target_event_id"] in self.cards:
            card = self.cards[m["target_event_id"]]
            if m["op"] == "void":
                action = self._action(card, "retract", m, m["cause"])
                action["previous_status"] = card["status"]
                card["status"] = "retracted"
                return [action]
            if "player" in m["changes"]:
                card["player"] = m["changes"]["player"]
                card["version"] += 1
                return [self._action(card, "rewrite", m, m["cause"])]
        return []

    def _moment(self, a: dict) -> dict | None:
        match, model = self.match, self.model
        kind = a["action_type"]
        if kind.startswith(SHOT_PREFIX):
            before = model.predict(match.state(a))
            if a["shot"]["outcome"] == "goal":
                if a["event_id"] in match.voided:  # its void overtook it: the goal never counted
                    return None
                after = model.predict(match.state(a, after=True))
                return self._card(a, "goal", swing(before, after), before, after, "after")
            if_goal = model.predict(match.state(a).with_goal(a["team"]))
            value = a["shot"]["xg"] * swing(before, if_goal)
            if value < self.threshold:
                return None
            if a["shot"]["outcome"] == "saved":  # the card credits the keeper: his save is the next message of the feed
                keeper = self.match.by_seq.get(a["seq"] + 1)
                if keeper is not None and keeper["type"] == "action" and keeper["action_type"] == "keeper_save":
                    return self._card(keeper, "save", value, before, if_goal, "if_goal", shot=a)  # it arrived first
                self.saves[a["seq"] + 1] = (a, value, before, if_goal)
                return None
            return self._card(a, "chance", value, before, if_goal, "if_goal")
        if kind == "foul" and a.get("card") in ("red", "second_yellow"):
            before, after = model.predict(match.state(a)), model.predict(match.state(a, after=True))
            return self._card(a, "red_card", swing(before, after), before, after, "after")
        return None

    def _card(self, a: dict, moment: str, value: float, before, other, other_name: str, shot: dict | None = None) -> dict:
        """A card about action `a`; for a save, `a` is the keeper's action and `shot` the shot he stopped."""
        team = a["team"]
        shot = shot or a
        score = self.match.score(shot["seq"])
        card = {
            "id": f"card-{a['event_id']}",
            "type": "card",
            "match_id": self.match.match_id,
            "event_id": a["event_id"],
            "shot_event_id": shot["event_id"] if "shot" in shot else None,
            "moment": moment,
            "status": "proposed",
            "version": 1,
            "under_review": False,
            "minute_label": a["minute_label"],
            "clock_s": a["clock_s"],
            "team": team,
            "team_name": a["team_name"],
            "player": a["player"],
            "score_before": score,
            "value_pts": pct(value),
            "facts": {
                "xg_pct": pct(shot["shot"]["xg"]) if "shot" in shot else None,
                "team_wp_before_pct": shown_pct(team_win(before, team)),
                f"team_wp_{other_name}_pct": shown_pct(team_win(other, team)),
                "draw_before_pct": shown_pct(before.draw),
                f"draw_{other_name}_pct": shown_pct(other.draw),
            },
        }
        self.cards[a["event_id"]] = card
        return card

    def _action(self, card: dict, action: str, m: dict, cause: str) -> dict:
        return {"id": f"{card['id']}-{m['message_id']}", "match_id": card["match_id"], "card_id": card["id"], "event_id": card["event_id"], "action": action, "cause": cause, "message_id": m["message_id"], "minute_label": m["minute_label"]}


def score_against_truth(cards: dict[str, dict], actions: list[dict], truth: dict) -> dict:
    """Recall of the moments the truth says deserve a card, precision of the cards, and the actions taken on cards the feed changed."""
    wanted = {g["event_id"] for g in truth["goals"]}
    wanted |= {m["event_id"] for m in truth["moments"] if m["kind"] in ("goal", "save") or (m["kind"] == "foul" and m["key"].startswith("red"))}
    found = {i for c in cards.values() for i in (c["event_id"], c["shot_event_id"])} & wanted
    expected = {g["event_id"]: g["expected_card_action"] for g in truth["goals"] if g["expected_card_action"] != "none"}
    done = {a["event_id"]: a["action"] for a in actions if a["action"] in ("retract", "rewrite")}
    return {
        "moments": len(wanted),
        "recall": len(found) / len(wanted) if wanted else 1.0,
        "cards": len(cards),
        "precision": len(found) / len(cards) if cards else 1.0,
        "missed": sorted(wanted - found),
        "card_actions_expected": expected,
        "card_actions_done": done,
        "card_actions_correct": all(done.get(k) == v for k, v in expected.items()) and set(done) <= set(expected),
    }
