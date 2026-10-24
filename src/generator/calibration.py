"""Counts from a feed, and the real orders of magnitude the engine is hand-tuned against.

The probabilities in the engine are set by hand, not fitted: the check is that the mean over many unscripted matches lands inside each band. Per match, both teams together.
"""

import math
import statistics

from generator.pitch import in_box

PASS_TYPES = {"pass", "cross", "goalkick", "freekick_short", "freekick_crossed", "corner_short", "corner_crossed"}  # throw-ins excluded
SHOT_TYPES = {"shot", "shot_freekick", "shot_penalty"}

# name: (low, high, real value and source)
TARGETS: dict[str, tuple[float, float, str]] = {
    "goals": (2.4, 3.3, "2.87, Premier League 2024/25"),
    "xg": (2.4, 3.3, "2.89, Premier League 2024/25 (FotMob)"),
    "shots": (22, 30, "about 26, Premier League 2024/25 (Opta)"),
    "shots_outside_box": (0.25, 0.38, "31.7 %, Premier League 2024/25 (Opta)"),
    "passes": (780, 1000, "about 893, Premier League 2024/25 (FootballCritic, FotMob)"),
    "pass_completion": (0.80, 0.87, "83.7 %, Premier League 2024/25 (FotMob)"),
    "pass_length": (16, 22, "20.5 m (22.4 yards), StatsBomb open data 2015/16, set pieces included"),
    "fouls": (19, 29, "25, StatsBomb open data 2015/16"),
    "yellow_cards": (3.2, 5.2, "4.2, Premier League 2023/24 (Opta)"),
    "tackles": (28, 46, "35 (FotMob 2024/25) to 42 (StatsBomb 2015/16)"),
    "interceptions": (13, 28, "16 (FotMob 2024/25) to 24 (StatsBomb 2015/16)"),
    "corners": (7, 13, "about 10 (rough, unverified)"),
    "offsides": (2, 6, "3 to 4 (rough, unverified)"),
    "added_minutes": (8, 13, "11 to 12 since 2023/24 (rough, unverified)"),
}


def count(stream: list[dict]) -> dict[str, float]:
    """What a consumer would count in a feed, duplicates dropped and voided goals excluded."""
    seen, unique = set(), []
    for m in stream:
        if m["message_id"] not in seen:
            seen.add(m["message_id"])
            unique.append(m)
    actions = [m for m in unique if m["type"] == "action"]
    voided = {m["target_event_id"] for m in unique if m["type"] == "correction" and m["op"] == "void"}
    passes = [a for a in actions if a["action_type"] in PASS_TYPES]
    shots = [a for a in actions if a["action_type"] in SHOT_TYPES]
    fouls = [a for a in actions if a["action_type"] == "foul"]

    def n(action_type: str) -> int:
        return sum(a["action_type"] == action_type for a in actions)

    def restarts(kind: str) -> int:
        return sum(a.get("restart") == kind for a in actions)

    # Possessions: a team keeps the ball for at least two actions in a row (a lone tackle, block or foul does not count). Not calibrated: StatsBomb (123 changes a match in 2015/16) merges brief spells by its own rules.
    spells: list[list] = []
    for a in sorted(actions, key=lambda a: a["seq"]):
        if spells and spells[-1][0] == a["team"]:
            spells[-1][1] += 1
        else:
            spells.append([a["team"], 1])
    held = [team for team, n_actions in spells if n_actions >= 2]
    return {
        "actions": len(actions),
        "goals": sum(a["shot"]["outcome"] == "goal" and a["event_id"] not in voided for a in shots),
        "xg": round(sum(a["shot"]["xg"] for a in shots), 2),
        "shots": len(shots),
        "shots_outside_box": round(sum(not in_box(a["start_x"], a["start_y"]) for a in shots) / max(len(shots), 1), 3),
        "passes": len(passes),
        "pass_completion": round(sum(a["result"] == "success" for a in passes) / max(len(passes), 1), 3),
        "pass_length": round(statistics.fmean(math.hypot(a["end_x"] - a["start_x"], a["end_y"] - a["start_y"]) for a in passes) if passes else 0.0, 1),
        "fouls": len(fouls),
        "yellow_cards": sum(a.get("card") in ("yellow", "second_yellow") for a in fouls),
        "red_cards": sum(a.get("card") in ("second_yellow", "red") for a in fouls),
        "tackles": n("tackle"),
        "interceptions": n("interception"),
        "take_ons": n("take_on"),
        "dribbles": n("dribble"),
        "clearances": n("clearance"),
        "throw_ins": n("throw_in"),
        "goal_kicks": n("goalkick"),
        "corners": restarts("corner"),
        "penalties": n("shot_penalty"),
        "offsides": sum(a["result"] == "offside" for a in passes),
        "possession_changes": sum(a != b for a, b in zip(held, held[1:])),
        "added_minutes": sum(m["minutes"] for m in unique if m["type"] == "added_time"),
        "substitutions": sum(m["type"] == "substitution" for m in unique),
        "var_checks": sum(m["type"] == "var_check" and m["status"] == "completed" for m in unique),
        "corrections": sum(m["type"] == "correction" for m in unique),
    }


def summarise(counts: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    """Mean, minimum and maximum of each count over many matches."""
    return {
        name: {"mean": statistics.fmean(c[name] for c in counts), "min": min(c[name] for c in counts), "max": max(c[name] for c in counts)}
        for name in counts[0]
    }
