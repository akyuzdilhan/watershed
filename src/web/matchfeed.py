"""The feed as the graphics read it: actions in provider order with every correction applied, plus match_info, substitutions and VAR checks.

Corrections: `update` and `reassign` merge their `changes` into the action (the VAR update adds `result: offside` and the `offside` margin to the pass, a reassignment changes the `player`); `void` keeps the action but marks it `voided: true` with its `void_cause`, so that a goal ruled out can still be drawn, as such. Duplicates are dropped by event id. Plain Python: the overlay and the worker can read it without plotly.
"""

import gzip
import json
from pathlib import Path


def load(path: Path) -> list[dict]:
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def build(messages: list[dict], upto_seq: int | None = None) -> dict:
    """`{info, actions, substitutions, var_checks, last_seq}` from raw messages, cut at `upto_seq` when given (what the desk has seen so far)."""
    msgs = sorted({m["message_id"]: m for m in messages if upto_seq is None or m.get("seq", 0) <= upto_seq}.values(), key=lambda m: m.get("seq", 0))
    info = next((m for m in msgs if m.get("type") == "match_info"), None)
    actions: dict[str, dict] = {}
    subs, var = [], []
    pending: list[dict] = []  # corrections that arrived before their action
    for m in msgs:
        t = m.get("type")
        if t == "action":
            actions.setdefault(m["event_id"], dict(m))
        elif t == "correction":
            pending.append(m)
        elif t == "substitution":
            subs.append(m)
        elif t == "var_check":
            var.append(m)
    for c in pending:
        a = actions.get(c.get("target_event_id"))
        if a is None:
            continue
        if c["op"] == "void":
            a.update(voided=True, void_cause=c.get("cause"), version=c.get("new_version", a.get("version")))
        else:
            a.update(c.get("changes") or {}, version=c.get("new_version", a.get("version")))
    return {
        "info": info, "actions": sorted(actions.values(), key=lambda a: a["seq"]), "substitutions": subs, "var_checks": var,
        "last_seq": msgs[-1]["seq"] if msgs else 0,
    }


def team_names(info: dict | None) -> dict[str, str]:
    teams = (info or {}).get("teams") or {}
    return {side: (teams.get(side) or {}).get("name", side.title()) for side in ("home", "away")}


def player_names(info: dict | None) -> dict[str, str]:
    teams = (info or {}).get("teams") or {}
    return {p["id"]: f"#{p['shirt']} {p['name']}" for team in teams.values() for p in team.get("starting", []) + team.get("bench", [])}


def is_goal(a: dict) -> bool:
    return bool(a.get("shot")) and a["shot"].get("outcome") == "goal"
