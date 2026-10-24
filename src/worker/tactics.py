"""Tactical details measured on the 360 freeze frames of the feed, and the post-match insight candidates built from them.

Every function is pure: it takes the feed's actions (corrections applied, as `web.matchfeed.build` returns them) and reads positions from each action's `freeze_frame`, which is in the acting team's frame (it attacks towards x = 105). Nothing here calls a model or Azure.

- `line_breaks`: completed passes and the outfield opponents they bypassed (standing between the passer and the point of reception along the pitch).
- `runs_behind`: passes played behind the defensive line (the second-last defender, keeper included, as the offside law and the generator count it), with the runner's margin.
- `space_at_reception`: distance from the receiver to the nearest opponent in the frame of the action that follows the pass.
- `pressure`: opponents within 5 m of the player on the ball.
- `line_heights`: each team's deepest outfield player, in metres from his own goal line.
- `insight_candidates`: documents of kind `tactical` per docs/contracts.md ("Lot 2 additions"), each with `headline_facts`, `field_meanings`, a within-match `rarity`, a `value` and a `figure` reference that `web.graphics.insight_figure` draws.

Limits: the frames come from the generator's `Shape`, not from tracking data. The nearest defender always closes the ball down to 1.2 to 5 m when he is within 15 m, attackers stay onside unless a run is planted, and locations are rounded to 0.1 m.
"""

import math
import statistics

LENGTH, WIDTH = 105.0, 68.0
HALF_X, MID_Y = LENGTH / 2, WIDTH / 2
PRESS_RADIUS_M = 5.0
RECEPTION_TOLERANCE_M = 2.0
PERIOD_START_S = {1: 0.0, 2: 2700.0, 3: 5400.0, 4: 6300.0}
PASS_TYPES = {"pass", "cross", "corner_crossed", "corner_short", "freekick_crossed", "freekick_short", "throw_in", "goalkick"}
RESTARTS = {"corner_crossed", "corner_short", "freekick_crossed", "freekick_short", "throw_in", "goalkick", "shot_freekick", "shot_penalty"}

# The value of a location: the xG of an unblocked footed shot from there, with the generator's provider xG constants (src/generator/pitch.py). Used as a simple "threat" surface, not a fitted expected-threat grid.
_XG_INTERCEPT, _XG_ANGLE, _XG_DISTANCE = -1.55, 1.55, -0.085
_POSTS = (MID_Y - 3.66, MID_Y + 3.66)


def location_value(x: float, y: float) -> float:
    dx = max(LENGTH - x, 0.05)
    angle = abs(math.atan2(_POSTS[1] - y, dx) - math.atan2(_POSTS[0] - y, dx))
    logit = _XG_INTERCEPT + _XG_ANGLE * angle + _XG_DISTANCE * math.hypot(LENGTH - x, MID_Y - y)
    return 1 / (1 + math.exp(-logit))


def threat_added(a: dict) -> float:
    """Value of the end location minus the start, for an action that keeps the ball (a completed pass or carry); 0 otherwise."""
    if a.get("result") != "success" or a.get("action_type") not in PASS_TYPES | {"dribble", "take_on"} or a.get("end_x") is None:
        return 0.0
    return location_value(a["end_x"], a["end_y"]) - location_value(a["start_x"], a["start_y"])


# --- frame helpers --------------------------------------------------------------------------------

def bucket(a: dict, minutes: int = 5) -> tuple[int, int]:
    """(period, index of the `minutes`-long slice since the period started); added time falls in the last slices of its period."""
    start = PERIOD_START_S.get(a.get("period", 1), 0.0)
    return a.get("period", 1), int(max(a["clock_s"] - start, 0) // (60 * minutes))


def frame_entry(a: dict, player_id: str) -> dict | None:
    return next((p for p in a.get("freeze_frame") or [] if p["player_id"] == player_id), None)


def actor_location(a: dict) -> tuple[float, float]:
    p = next((p for p in a.get("freeze_frame") or [] if p.get("actor")), None)
    return tuple(p["location"]) if p else (a["start_x"], a["start_y"])


def opponents(a: dict, outfield: bool = True) -> list[dict]:
    return [p for p in a.get("freeze_frame") or [] if not p["teammate"] and not (outfield and p.get("keeper"))]


def offside_line(a: dict) -> tuple[float, str | None]:
    """The second-last defender (keeper included) in the acting team's frame, as `Shape.line` and `web.pitch.offside_line` compute it."""
    xs = sorted(((p["location"][0], p["player_id"]) for p in opponents(a, outfield=False)), reverse=True)
    return (xs[1][0], xs[1][1]) if len(xs) > 1 else (HALF_X, None)


def counted_goal(a: dict) -> bool:
    return bool(a.get("shot")) and a["shot"].get("outcome") == "goal" and not a.get("voided")


def any_goal(a: dict) -> bool:
    return bool(a.get("shot")) and a["shot"].get("outcome") == "goal"


def chain(actions: list[dict], event_id: str) -> list[dict]:
    """The actions of the possession that ends with `event_id` (included), in order."""
    i = next(k for k, a in enumerate(actions) if a["event_id"] == event_id)
    j = i
    while j > 0 and actions[j - 1].get("possession") == actions[i].get("possession") and actions[j - 1].get("period") == actions[i].get("period"):
        j -= 1
    return actions[j:i + 1]


# --- detectors ------------------------------------------------------------------------------------

def receptions(actions: list[dict]) -> list[tuple[dict, dict]]:
    """(pass, next action) for every completed pass whose next action is a team-mate's, starting where the pass ended."""
    out = []
    for a, b in zip(actions, actions[1:]):
        if a.get("action_type") in PASS_TYPES and a.get("result") == "success" and b["team"] == a["team"] and b.get("period") == a.get("period") \
                and math.hypot(b["start_x"] - a["end_x"], b["start_y"] - a["end_y"]) <= RECEPTION_TOLERANCE_M:
            out.append((a, b))
    return out


def defenders_bypassed(pass_action: dict) -> list[str]:
    """Outfield opponents standing strictly between the passer and the end of the pass along the pitch (x), in the pass's frame."""
    sx, ex = pass_action["start_x"], pass_action["end_x"]
    if ex <= sx:
        return []
    return sorted(p["player_id"] for p in opponents(pass_action) if sx < p["location"][0] < ex)


def line_breaks(actions: list[dict], min_bypassed: int = 1) -> list[dict]:
    """Completed passes that bypassed at least `min_bypassed` outfield opponents. Keepers' passes are left out: a long ball from the keeper bypasses everyone by construction."""
    rows = []
    for a, b in receptions(actions):
        if any(p.get("actor") and p.get("keeper") for p in a.get("freeze_frame") or []):
            continue
        ids = defenders_bypassed(a)
        if len(ids) >= min_bypassed:
            rows.append({"event_id": a["event_id"], "reception_event_id": b["event_id"], "seq": a["seq"], "minute_label": a.get("minute_label"), "clock_s": a["clock_s"],
                         "period": a.get("period"), "team": a["team"], "passer_id": a["player"]["id"], "receiver_id": b["player"]["id"], "bypassed": len(ids), "bypassed_ids": ids,
                         "length_m": round(math.hypot(a["end_x"] - a["start_x"], a["end_y"] - a["start_y"]), 1), "technique": a.get("technique")})
    return rows


def runs_behind(actions: list[dict]) -> list[dict]:
    """Passes in the opposition half that end beyond the offside line, with the runner (the player named by a VAR offside update, else the team-mate nearest the end of the pass) and his margin at the pass: positive beyond the line (offside position), negative onside."""
    rows = []
    for a in actions:
        if a.get("action_type") not in PASS_TYPES or a.get("end_x") is None or not a.get("freeze_frame"):
            continue
        line_x, defender = offside_line(a)
        if line_x <= HALF_X or a["end_x"] <= line_x:
            continue
        official = a.get("offside") or {}
        mates = [p for p in a["freeze_frame"] if p["teammate"] and not p.get("actor") and not p.get("keeper")]
        if not mates:
            continue
        runner = next((p for p in mates if p["player_id"] == official.get("player_id")), None) or \
            min(mates, key=lambda p: math.hypot(p["location"][0] - a["end_x"], p["location"][1] - a["end_y"]))
        frame_margin = runner["location"][0] - line_x
        rows.append({"event_id": a["event_id"], "seq": a["seq"], "minute_label": a.get("minute_label"), "clock_s": a["clock_s"], "period": a.get("period"), "team": a["team"],
                     "passer_id": a["player"]["id"], "runner_id": runner["player_id"], "line_x": line_x, "defender_id": defender, "runner_x": runner["location"][0],
                     "frame_margin_m": round(frame_margin, 2), "margin_m": round(official.get("margin_m", frame_margin), 2), "official": "margin_m" in official,
                     "ball_beyond_m": round(a["end_x"] - line_x, 1), "result": a.get("result")})
    return rows


def space_at_reception(actions: list[dict]) -> list[dict]:
    rows = []
    for a, b in receptions(actions):
        opp = opponents(b, outfield=False)
        if not opp:
            continue
        rx, ry = actor_location(b)
        near = min(opp, key=lambda p: math.hypot(p["location"][0] - rx, p["location"][1] - ry))
        rows.append({"pass_event_id": a["event_id"], "event_id": b["event_id"], "seq": b["seq"], "minute_label": b.get("minute_label"), "clock_s": b["clock_s"],
                     "period": b.get("period"), "team": b["team"], "receiver_id": b["player"]["id"], "receiver_xy": [rx, ry], "nearest_id": near["player_id"],
                     "nearest_xy": list(near["location"]), "space_m": round(math.hypot(near["location"][0] - rx, near["location"][1] - ry), 1)})
    return rows


def pressure(actions: list[dict], radius_m: float = PRESS_RADIUS_M) -> list[dict]:
    """For every open-play action with a frame: the opponents within `radius_m` of the player on the ball. `pressing_team` is the side without the ball."""
    rows = []
    for a in actions:
        if not a.get("freeze_frame") or a.get("action_type") in RESTARTS or a.get("restart"):
            continue
        x, y = actor_location(a)
        ids = sorted(p["player_id"] for p in opponents(a, outfield=False) if math.hypot(p["location"][0] - x, p["location"][1] - y) <= radius_m)
        rows.append({"event_id": a["event_id"], "seq": a["seq"], "clock_s": a["clock_s"], "period": a.get("period"), "minute_label": a.get("minute_label"),
                     "team": a["team"], "pressing_team": "away" if a["team"] == "home" else "home", "pressers": len(ids), "presser_ids": ids})
    return rows


def line_heights(actions: list[dict]) -> list[dict]:
    """Per action, each team's deepest outfield player in metres from his own goal line, and which team had the ball."""
    rows = []
    for a in actions:
        frame = a.get("freeze_frame")
        if not frame:
            continue
        mates = [p["location"][0] for p in frame if p["teammate"] and not p.get("keeper")]
        opp = [p["location"][0] for p in frame if not p["teammate"] and not p.get("keeper")]
        if not mates or not opp:
            continue
        other = "away" if a["team"] == "home" else "home"
        rows.append({"event_id": a["event_id"], "seq": a["seq"], "clock_s": a["clock_s"], "period": a.get("period"), "minute_label": a.get("minute_label"), "in_possession": a["team"],
                     a["team"]: round(min(mates), 1), other: round(LENGTH - max(opp), 1)})
    return rows


def per_bucket(rows: list[dict], value, minutes: int = 5, agg=statistics.fmean) -> list[dict]:
    """`[{period, index, start_min, value, n}]`: `value(row)` (None to skip) aggregated per slice of `minutes` within each period."""
    groups: dict[tuple[int, int], list[float]] = {}
    for r in rows:
        v = value(r)
        if v is not None:
            groups.setdefault(bucket(r, minutes), []).append(v)
    out = []
    for (period, k), vals in sorted(groups.items()):
        out.append({"period": period, "index": k, "start_min": PERIOD_START_S.get(period, 0) / 60 + k * minutes, "value": agg(vals), "n": len(vals)})
    return out


# --- insight candidates ---------------------------------------------------------------------------

def _names(info: dict | None) -> tuple[dict[str, str], dict[str, str]]:
    teams = (info or {}).get("teams") or {}
    players = {p["id"]: p["name"] for t in teams.values() for p in t.get("starting", []) + t.get("bench", [])}
    shirts = {p["id"]: p["shirt"] for t in teams.values() for p in t.get("starting", []) + t.get("bench", [])}
    return {side: (teams.get(side) or {}).get("name", side.title()) for side in ("home", "away")}, {pid: f"#{shirts[pid]} {n}" for pid, n in players.items()}


def _rarity(at_least: int, total: int, what: str) -> dict:
    p = at_least / total if total else 1.0
    return {"p": round(p, 4), "surprisal_bits": round(-math.log2(p), 2) if p > 0 else None, "basis": what}


def _doc(match_id: str, key: str, anchor: dict, facts: dict, meanings: dict, rarity: dict, value: int, figure: dict) -> dict:
    missing = set(facts) - set(meanings)
    if missing:
        raise ValueError(f"facts without a meaning: {sorted(missing)}")
    return {"id": f"insight-{match_id}-{key}", "type": "insight", "match_id": match_id, "key": key, "kind": "tactical", "minute_label": anchor.get("minute_label"),
            "clock_s": anchor.get("clock_s"), "event_id": anchor.get("event_id"), "headline_facts": facts, "field_meanings": meanings, "rarity": rarity,
            "value": max(0, min(100, int(round(value)))), "status": "proposed", "version": 1, "history": [], "figure": figure}


def insight_candidates(actions: list[dict], info: dict | None = None, match_id: str | None = None, limit: int = 6) -> list[dict]:
    """The post-match "detail that changed everything" candidates, best first. Each one names a goal (counted or ruled out) or a team-wide shift, with every number in `headline_facts`."""
    if not actions:
        return []
    match_id = match_id or actions[0].get("match_id", "match")
    teams, names = _names(info)
    name = lambda pid: names.get(pid, pid)  # noqa: E731
    goals = [a for a in actions if any_goal(a)]
    out: list[dict] = []

    # 1. the pass that bypassed the most defenders in the build-up of a goal
    breaks = line_breaks(actions)
    completed = len(receptions(actions))
    best_by_goal = []
    for g in goals:
        ids = {a["event_id"] for a in chain(actions, g["event_id"])}
        mine = [r for r in breaks if r["event_id"] in ids]
        if mine:
            best_by_goal.append((max(mine, key=lambda r: (r["bypassed"], r["length_m"])), g))
    best_by_goal.sort(key=lambda t: (-t[0]["bypassed"], -t[0]["length_m"]))
    for r, g in best_by_goal[:2]:
        if r["bypassed"] < 3:
            continue
        n_at_least = sum(1 for b in breaks if b["bypassed"] >= r["bypassed"])
        facts = {"pass_minute_label": r["minute_label"], "team_name": teams[r["team"]], "passer_name": name(r["passer_id"]), "receiver_name": name(r["receiver_id"]),
                 "defenders_bypassed": r["bypassed"], "pass_length_m": r["length_m"], "goal_minute_label": g.get("minute_label"), "scorer_name": name(g["player"]["id"]),
                 "goal_stood": not g.get("voided"), "completed_passes_in_match": completed, "passes_bypassing_as_many": n_at_least}
        meanings = {"pass_minute_label": "match clock of the pass", "team_name": "team that made the pass", "passer_name": "player who made the pass",
                    "receiver_name": "player who received it", "defenders_bypassed": "outfield opponents standing between the passer and the point of reception along the pitch, in the frame of the pass",
                    "pass_length_m": "length of the pass in metres", "goal_minute_label": "match clock of the goal that followed in the same possession",
                    "scorer_name": "player credited with that goal", "goal_stood": "false when VAR ruled the goal out",
                    "completed_passes_in_match": "completed passes received by a team-mate in this match, both teams",
                    "passes_bypassing_as_many": "completed passes in this match that bypassed at least as many defenders, this one included"}
        rarity = _rarity(n_at_least, completed, f"{r['bypassed']} defenders bypassed: {n_at_least} of {completed} completed passes in this match")
        out.append(_doc(match_id, f"line-break-{r['event_id']}", r, facts, meanings, rarity, 35 + 7 * r["bypassed"] + (10 if not g.get("voided") else 5),
                        {"kind": "line_break", "event_id": r["event_id"], "goal_event_id": g["event_id"]}))

    # 2. the tightest run behind the line before a goal
    runs = runs_behind(actions)
    run_by_goal = []
    for g in goals:
        ids = {a["event_id"] for a in chain(actions, g["event_id"])}
        mine = [r for r in runs if r["event_id"] in ids]
        if mine:
            run_by_goal.append((min(mine, key=lambda r: abs(r["margin_m"])), g))
    run_by_goal.sort(key=lambda t: abs(t[0]["margin_m"]))
    for r, g in run_by_goal[:2]:
        if abs(r["margin_m"]) > 1.5:
            continue
        n_tighter = sum(1 for x in runs if abs(x["margin_m"]) <= abs(r["margin_m"]))
        offside = r["margin_m"] > 0
        facts = {"pass_minute_label": r["minute_label"], "team_name": teams[r["team"]], "runner_name": name(r["runner_id"]), "passer_name": name(r["passer_id"]),
                 "margin_m": abs(r["margin_m"]), "runner_beyond_line": offside, "line_defender_name": name(r["defender_id"]) if r["defender_id"] else "",
                 "goal_minute_label": g.get("minute_label"), "goal_stood": not g.get("voided"), "runs_behind_line_in_match": len(runs)}
        meanings = {"pass_minute_label": "match clock of the pass played behind the line", "team_name": "attacking team", "runner_name": "player running onto the pass",
                    "passer_name": "player who played the pass",
                    "margin_m": "distance in metres between the runner and the second-last defender at the moment of the pass" + (" (official VAR figure)" if r["official"] else " (measured on the freeze frame)"),
                    "runner_beyond_line": "true when the runner was beyond the second-last defender (offside position), false when he was onside",
                    "line_defender_name": "the second-last defender, who sets the offside line", "goal_minute_label": "match clock of the goal that followed",
                    "goal_stood": "false when VAR ruled the goal out", "runs_behind_line_in_match": "passes in the opposition half played beyond the defensive line in this match, both teams"}
        rarity = _rarity(n_tighter, len(runs), f"runner {abs(r['margin_m']):.2f} m {'beyond' if offside else 'inside'} the line: {n_tighter} of {len(runs)} passes behind the line in this match were as tight")
        value = 90 if offside and g.get("voided") else 75 if not g.get("voided") else 60
        out.append(_doc(match_id, f"run-{r['event_id']}", r, facts, meanings, rarity, value - 10 * abs(r["margin_m"]),
                        {"kind": "run", "event_id": r["event_id"], "runner_id": r["runner_id"], "goal_event_id": g["event_id"]}))

    # 3. the scorer with the most space when he received the ball
    spaces = space_at_reception(actions)
    space_of = {s["event_id"]: s for s in spaces}
    median_space = round(statistics.median(s["space_m"] for s in spaces), 1) if spaces else 0.0
    scorer_space = []
    for g in goals:
        s = space_of.get(g["event_id"])  # the goal was the scorer's first touch
        if s is None:
            k = actions.index(g)
            prev = actions[k - 1] if k else None
            s = space_of.get(prev["event_id"]) if prev and prev["player"]["id"] == g["player"]["id"] else None
        if s is not None:
            scorer_space.append((s, g))
    if scorer_space:
        s, g = max(scorer_space, key=lambda t: t[0]["space_m"])
        if s["space_m"] >= 2 * max(median_space, 0.1):
            n_more = sum(1 for x in spaces if x["space_m"] >= s["space_m"])
            facts = {"reception_minute_label": s["minute_label"], "team_name": teams[s["team"]], "scorer_name": name(s["receiver_id"]), "space_m": s["space_m"],
                     "nearest_opponent_name": name(s["nearest_id"]), "match_median_space_m": median_space, "goal_minute_label": g.get("minute_label"), "goal_stood": not g.get("voided")}
            meanings = {"reception_minute_label": "match clock when the scorer received the ball", "team_name": "scoring team", "scorer_name": "player who received and scored",
                        "space_m": "distance in metres from the scorer to the nearest opponent (keeper included) when he received the ball",
                        "nearest_opponent_name": "that nearest opponent", "match_median_space_m": "median of the same distance over every reception of the match",
                        "goal_minute_label": "match clock of the goal", "goal_stood": "false when VAR ruled the goal out"}
            rarity = _rarity(n_more, len(spaces), f"{s['space_m']:.1f} m of space at reception: {n_more} of {len(spaces)} receptions in this match had as much")
            out.append(_doc(match_id, f"space-{g['event_id']}", s, facts, meanings, rarity, 40 + 4 * min(s["space_m"], 10),
                            {"kind": "space", "event_id": s["event_id"], "goal_event_id": g["event_id"]}))

    # 4. a team whose defensive line moved between the halves, and 5. a team that pressed harder or less
    heights = line_heights(actions)
    press = pressure(actions)
    for side in ("home", "away"):
        def half(rows, period, key, cond):
            vals = [r[key] for r in rows if r.get("period") == period and cond(r)]
            return (statistics.median(vals), len(vals)) if vals else (None, 0)
        h1, _ = half(heights, 1, side, lambda r: r["in_possession"] != side)
        h2, _ = half(heights, 2, side, lambda r: r["in_possession"] != side)
        if h1 is not None and h2 is not None and abs(h2 - h1) >= 3.0:
            first = next(r for r in heights if r.get("period") == 2)
            facts = {"team_name": teams[side], "first_half_line_m": round(h1, 1), "second_half_line_m": round(h2, 1), "change_m": round(abs(h2 - h1), 1),
                     "line_moved": "higher" if h2 > h1 else "deeper"}
            meanings = {"team_name": "defending team", "first_half_line_m": "median distance in metres from its own goal line to its deepest outfield player when the opponent had the ball, first half",
                        "second_half_line_m": "the same, second half", "change_m": "difference between the two, in metres", "line_moved": "direction of the change"}
            out.append(_doc(match_id, f"line-height-{side}", first, facts, meanings, {"p": None, "surprisal_bits": None, "basis": "medians over every frame of each half in this match"},
                            30 + 3 * abs(h2 - h1), {"kind": "line_height", "team": side}))
        p1 = [r["pressers"] for r in press if r["pressing_team"] == side and r.get("period") == 1]
        p2 = [r["pressers"] for r in press if r["pressing_team"] == side and r.get("period") == 2]
        if p1 and p2:
            m1, m2 = statistics.fmean(p1), statistics.fmean(p2)
            if m1 > 0 and abs(m2 - m1) / m1 >= 0.2:
                first = next(r for r in press if r.get("period") == 2)
                facts = {"team_name": teams[side], "first_half_pressers": round(m1, 2), "second_half_pressers": round(m2, 2), "change_pct": round(100 * abs(m2 - m1) / m1),
                         "pressed": "harder" if m2 > m1 else "less"}
                meanings = {"team_name": "pressing team", "first_half_pressers": "mean number of its players within 5 m of the opponent on the ball, first half",
                            "second_half_pressers": "the same, second half", "change_pct": "relative change in percent", "pressed": "direction of the change"}
                out.append(_doc(match_id, f"pressing-{side}", first, facts, meanings, {"p": None, "surprisal_bits": None, "basis": "means over every open-play frame of each half in this match"},
                                30 + 0.4 * facts["change_pct"], {"kind": "pressing", "team": side}))

    out.sort(key=lambda d: -d["value"])
    return out[:limit]
