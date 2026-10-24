"""Export a generated match to the Common Data Format (CDF).

CDF is an open format for football match data (Anzer et al., 2025, "Common Data Format (CDF): a Standardized Format for Match-Data in Football (Soccer)"); its JSON schemas and validator are published under the MIT licence (common-data-format-validator). The export reads the feed alone, applies its corrections, and writes the match as it finally stands:

- `<match>.meta.json`: competition, teams and line-ups, periods, whistles, data sources
- `<match>.match.json`: the match sheet (result, goals, substitutions, cards, officials)
- `<match>.event.jsonl`: one event per action, plus referee events for cards, offsides and substitutions
- `<match>.tracking.jsonl`: one frame of the 22 players and the ball at the start of each action (when the feed carries freeze frames)

The live feed keeps its own envelope (sequence numbers, versions, corrections): CDF describes data at rest, so a disallowed goal appears here as a shot reviewed by VAR and not upheld, and a reassigned goal under its final scorer. Coordinates are in metres from the centre spot, x along the sidelines, with the home team playing left to right in the first half.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from generator.pitch import HALF_X, MID_Y, flip

CDF_VERSION = "0.3.2"
PERIODS = {1: "first_half", 2: "second_half"}
BODY_PARTS = {"foot_left": "left_foot", "foot_right": "right_foot", "foot": "feet", "head": "head", "other": "other"}
SHOT_OUTCOMES = {"goal": "successful", "saved": "saved", "blocked": "blocked", "off_target": "wide", "post": "woodwork"}
PASS_SUB_TYPES = {"throw_in": "throw_in", "goalkick": "goal_kick", "freekick_short": "free_kick", "freekick_crossed": "free_kick", "corner_short": "corner_kick", "corner_crossed": "corner_kick", "cross": "cross"}
SHOT_SUB_TYPES = {"shot_freekick": "free_kick", "shot_penalty": "penalty_kick"}
PASSES = {"pass", "cross", *PASS_SUB_TYPES}
CARDS = {"yellow": "yellow_card", "second_yellow": "second_yellow_card", "red": "red_card"}
REFEREE_ID = "referee_1"


def cdf_id(value: str) -> str:
    """CDF wants snake_case identifiers."""
    return value.replace("-", "_").lower()


class CdfWriter:
    def __init__(self, stream: list[dict]) -> None:
        first: dict[str, dict] = {}
        for m in stream:
            first.setdefault(m["message_id"], m)
        self.msgs = sorted(first.values(), key=lambda m: m["seq"])
        self.info = next(m for m in self.msgs if m["type"] == "match_info")
        self.match_id = cdf_id(self.info["match_id"])
        self.kickoff = datetime.fromisoformat(self.info["kickoff_utc"].replace("Z", "+00:00"))
        self.team_ids = {side: cdf_id(t["name"]) for side, t in self.info["teams"].items()}
        self.actions = [dict(m) for m in self.msgs if m["type"] == "action"]
        self.voided, self.reviewed = self._apply_corrections()

    def _apply_corrections(self) -> tuple[set[str], dict[str, bool]]:
        by_id = {a["event_id"]: a for a in self.actions}
        voided = set()
        for m in self.msgs:
            if m["type"] != "correction":
                continue
            target = by_id[m["target_event_id"]]
            if m["op"] == "void":
                voided.add(target["event_id"])
            else:  # reassign and update both replace fields
                target.update(m["changes"])
        reviewed = {}  # goal event id -> upheld
        for m in self.msgs:
            if m["type"] == "var_check" and m["status"] == "completed":
                reviewed[m["target_event_id"]] = m["decision"] == "stands"
        return voided, reviewed

    # --- helpers --------------------------------------------------------------------------------

    def time(self, wall_s: float) -> str:
        return (self.kickoff + timedelta(seconds=wall_s)).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def clock(clock_s: float) -> str:
        minutes, seconds = divmod(clock_s, 60)
        return f"{int(minutes):02d}:{seconds:05.2f}"

    def xy(self, team: str, period: int, x: float, y: float) -> tuple[float, float]:
        """Acting team's frame to the CDF pitch frame."""
        hx, hy = (x, y) if team == "home" else flip(x, y)
        if period == 2:
            hx, hy = flip(hx, hy)
        return round(hx - HALF_X, 2), round(hy - MID_Y, 2)

    def player(self, ref: dict) -> str:
        return cdf_id(ref["id"])

    # --- files ----------------------------------------------------------------------------------

    def events(self) -> list[dict]:
        out = []
        frames = self.info.get("freeze_frames", False)
        for i, a in enumerate(self.actions):
            nxt = self.actions[i + 1] if i + 1 < len(self.actions) else None
            out.append(self._event(a, nxt, i if frames else None))
            if a["action_type"] == "foul" and a.get("card"):
                out.append(self._referee(a, "caution", CARDS[a["card"]], [cdf_id(a["event_id"])]))
            if a["result"] == "offside":
                out.append(self._referee(a, "offside", "offside", [cdf_id(a["event_id"])]))
        for m in self.msgs:
            if m["type"] == "substitution":
                out.append(self._referee(m, "substitution", "end", None, suffix="sub"))
        out.sort(key=lambda e: (e["event"]["time"], e["event"]["id"]))
        return out

    def _event(self, a: dict, nxt: dict | None, frame_id: int | None) -> dict:
        kind, success = a["action_type"], a["result"] == "success"
        x, y = self.xy(a["team"], a["period"], a["start_x"], a["start_y"])
        x_end, y_end = self.xy(a["team"], a["period"], a["end_x"], a["end_y"])
        receiver = nxt if success and nxt is not None and nxt["team"] == a["team"] and kind in PASSES else None
        metrics = None
        var = None
        if kind in PASSES:
            ev_type = "pass"
            sub_type = "kick_off" if a.get("restart") == "kick_off" else PASS_SUB_TYPES.get(kind)
            if success:
                outcome = "successful"
            elif a["result"] == "offside":
                outcome = "offside"
            elif nxt is not None and nxt["action_type"] == "interception":
                outcome = "intercepted"
            elif nxt is not None and nxt.get("restart") and nxt["team"] != a["team"]:
                outcome = "out_of_play"
            else:
                outcome = "unsuccessful"
        elif kind.startswith("shot"):
            ev_type, sub_type = "shot", SHOT_SUB_TYPES.get(kind)
            outcome = SHOT_OUTCOMES[a["shot"]["outcome"]]
            success = a["shot"]["outcome"] == "goal" and a["event_id"] not in self.voided
            metrics = {"xg": a["shot"]["xg"]}
            if a["event_id"] in self.reviewed:
                var = {"reviewed": True, "upheld": self.reviewed[a["event_id"]]}
        elif kind in ("interception", "clearance"):
            ev_type, sub_type, outcome = "defending", kind, kind
        elif kind in ("keeper_save", "keeper_claim"):
            ev_type, sub_type = "goalkeeping", kind.removeprefix("keeper_")
            outcome = sub_type
        else:  # dribble (a carry), take_on, tackle, foul
            ev_type, sub_type = "misc", {"dribble": "carry"}.get(kind, kind)
            outcome = "successful" if success else "unsuccessful"
        event = {
            "id": cdf_id(a["event_id"]),
            "time": self.time(a["wall_s"]),
            "period": PERIODS[a["period"]],
            "type": ev_type,
            "sub_type": sub_type,
            "is_successful": success,
            "outcome_type": outcome,
            "player_id": self.player(a["player"]),
            "team_id": self.team_ids[a["team"]],
            "receiver_id": self.player(receiver["player"]) if receiver else None,
            "receiver_time": self.time(receiver["wall_s"]) if receiver else None,
            "receiver_team_id": self.team_ids[receiver["team"]] if receiver else None,
            "x": x,
            "y": y,
            "x_end": x_end if ev_type in ("pass", "shot") or (x_end, y_end) != (x, y) else None,
            "y_end": y_end if ev_type in ("pass", "shot") or (x_end, y_end) != (x, y) else None,
            "body_part": BODY_PARTS.get(a["bodypart"]),
            "related_event_ids": None,
            "match_clock": self.clock(a["clock_s"]),
        }
        if metrics:
            event["metrics"] = metrics
        if var:
            event["var"] = var
        record = {"match": {"id": self.match_id}, "meta": {"is_synced": frame_id is not None}, "event": event}
        if frame_id is not None:
            record["tracking"] = {"frame_id": frame_id}
        return record

    def _referee(self, m: dict, sub_type: str, outcome: str, related: list[str] | None, suffix: str = "ref") -> dict:
        event = {
            "id": cdf_id(m["message_id"]) + f"_{suffix}",
            "time": self.time(m["wall_s"]),
            "period": PERIODS[m["period"]],
            "type": "referee",
            "sub_type": sub_type,
            "is_successful": True,
            "outcome_type": outcome,
            "official_id": REFEREE_ID,
            "receiver_id": None,
            "receiver_time": None,
            "receiver_team_id": None,
            "x": 0.0,
            "y": 0.0,
            "x_end": None,
            "y_end": None,
            "body_part": None,
            "related_event_ids": related,
            "match_clock": self.clock(m["clock_s"]),
        }
        if "start_x" in m:
            event["x"], event["y"] = self.xy(m["team"], m["period"], m["start_x"], m["start_y"])
        return {"match": {"id": self.match_id}, "meta": {"is_synced": False}, "event": event}

    def tracking(self) -> list[dict]:
        frames = []
        for i, a in enumerate(self.actions):
            teams = {side: {"id": self.team_ids[side], "players": []} for side in ("home", "away")}
            for p in a.get("freeze_frame", []):
                side = a["team"] if p["teammate"] else ("away" if a["team"] == "home" else "home")
                x, y = self.xy(a["team"], a["period"], *p["location"])
                teams[side]["players"].append({"id": cdf_id(p["player_id"]), "x": x, "y": y})
            bx, by = self.xy(a["team"], a["period"], a["start_x"], a["start_y"])
            frames.append({
                "frame_id": i,
                "timestamp": self.time(a["wall_s"]),
                "period": PERIODS[a["period"]],
                "match": {"id": self.match_id},
                "teams": teams,
                "ball": {"x": bx, "y": by, "z": 0.0, "status": True, "poss_team_id": self.team_ids[a["team"]]},
            })
        return frames

    def _periods(self) -> tuple[list[dict], list[dict]]:
        periods, whistles = [], []
        for n in (1, 2):
            start = next(m for m in self.msgs if m["type"] == "period_start" and m["period"] == n)
            end = next(m for m in self.msgs if m["type"] == "period_end" and m["period"] == n)
            left, right = ("home", "away") if n == 1 else ("away", "home")
            periods.append({
                "period": PERIODS[n], "play_direction": "left_right" if n == 1 else "right_left",
                "start_time": self.time(start["wall_s"]), "end_time": self.time(end["wall_s"]),
                "left_team_id": self.team_ids[left], "right_team_id": self.team_ids[right],
            })
            whistles += [
                {"type": PERIODS[n], "sub_type": "start", "time": self.time(start["wall_s"])},
                {"type": PERIODS[n], "sub_type": "end", "time": self.time(end["wall_s"])},
            ]
        return periods, whistles

    def _players(self, side: str) -> list[dict]:
        t = self.info["teams"][side]
        players = []
        for starter, group in ((True, t["starting"]), (False, t["bench"])):
            for p in group:
                first, _, last = p["name"].partition(" ")
                players.append({"id": cdf_id(p["id"]), "team_id": self.team_ids[side], "jersey_number": p["shirt"], "is_starter": starter, "first_name": first, "last_name": last})
        return players

    def meta(self) -> dict:
        periods, whistles = self._periods()
        source = {"version": self.info.get("generator_version", ""), "name": "watershed_generator"}
        return {
            "competition": {"id": "synthetic_league", "name": self.info["competition"]},
            "season": {"id": "season_2026_27", "name": "2026/27"},
            "match": {"id": self.match_id, "kickoff_time": self.time(0.0), "periods": periods, "whistles": whistles},
            "teams": {side: {"id": self.team_ids[side], "players": self._players(side)} for side in ("home", "away")},
            "venue": {"id": "synthetic_ground", "pitch_length": self.info["pitch"]["length_m"], "pitch_width": self.info["pitch"]["width_m"], "name": "Synthetic Ground"},
            "meta": {
                "video": None,
                "tracking": {**source, "fps": 0, "collection_timing": "post_match"} if self.info.get("freeze_frames") else None,
                "event": {**source, "collection_timing": "live"},
                "landmarks": None,
                "meta": source,
                "ball": None,
                "cdf": {"version": CDF_VERSION},
            },
        }

    def match_sheet(self) -> dict:
        goals, score = [], {"home": 0, "away": 0}
        half_scores = {}
        prev_pass = {}
        for a in self.actions:
            if a["action_type"].startswith("shot") and a["shot"]["outcome"] == "goal" and a["event_id"] not in self.voided:
                score[a["team"]] += 1
                assist = prev_pass.get(a["team"])
                goals.append({
                    "time": self.time(a["wall_s"]), "period": PERIODS[a["period"]], "team_id": self.team_ids[a["team"]], "player_id": self.player(a["player"]),
                    "assist_id": assist if assist and assist != self.player(a["player"]) else None,
                    "is_own_goal": False, "is_penalty": a["action_type"] == "shot_penalty", "score": dict(score),
                })
            if a["action_type"] in PASSES and a["result"] == "success":
                prev_pass = {a["team"]: self.player(a["player"])}
            elif a["action_type"] not in ("dribble",):
                prev_pass = {} if not a["action_type"].startswith("shot") else prev_pass
            half_scores[a["period"]] = dict(score)
        subs = [
            {"team_id": self.team_ids[m["team"]], "in_time": self.time(m["wall_s"]), "out_time": self.time(m["wall_s"]), "period": PERIODS[m["period"]],
             "in_player_id": self.player(m["player_in"]), "out_player_id": self.player(m["player_out"])}
            for m in self.msgs if m["type"] == "substitution"
        ]
        cards = [
            {"team_id": self.team_ids[a["team"]], "time": self.time(a["wall_s"]), "period": PERIODS[a["period"]], "player_id": self.player(a["player"]), "type": CARDS[a["card"]]}
            for a in self.actions if a["action_type"] == "foul" and a.get("card")
        ]
        final = dict(score)
        winner = None if final["home"] == final["away"] else self.team_ids["home" if final["home"] > final["away"] else "away"]
        teams = {}
        for side in ("home", "away"):
            t = self.info["teams"][side]
            teams[side] = {"id": self.team_ids[side], "short_name": t["name"], "formation": t["formation"], "players": [{**p, "has_played": True} for p in self._players(side)]}
        return {
            "match": {
                "id": self.match_id,
                "status": {"is_neutral": False, "has_extratime": False, "has_shootout": False},
                "result": {"final": {**final, "winning_team_id": winner}, "first_half": half_scores.get(1, {"home": 0, "away": 0}), "second_half": final},
            },
            "teams": teams,
            "officials": [{"id": REFEREE_ID, "type": "main_referee", "first_name": self.info["referee"]["name"].split(" ")[0], "last_name": self.info["referee"]["name"].split(" ", 1)[1]}],
            "events": {"goals": goals, "substitutions": subs, "cards": cards},
            "meta": {"vendor": "Watershed synthetic generator"},
        }


def write_cdf(stream: list[dict], out: Path) -> list[Path]:
    """Writes the four CDF files of the match in `out` and returns their paths."""
    w = CdfWriter(stream)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, data in (("meta", w.meta()), ("match", w.match_sheet())):
        path = out / f"{w.match_id}.{name}.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        paths.append(path)
    lines = {"event": w.events()}
    if w.info.get("freeze_frames"):
        lines["tracking"] = w.tracking()
    for name, records in lines.items():
        path = out / f"{w.match_id}.{name}.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        paths.append(path)
    return paths
