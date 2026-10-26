"""The desk as a read-only MCP server ("watershed-desk"): the facts of a card, the match so far, the pre-match storyline and the league history, each as JSON with the meaning of every field.

Every value is computed by code; agents cite numbers by field name and the Verifier checks them. Served in process to the worker's agents (`mcp.Client(server)`), or on stdio for any MCP host:

    python -m worker.mcp_server --desk-dir data/desk [--history data/history/9]

The `mcp` and `pydantic` imports live in `build_server` and `main`, so the fact functions import without them.
"""

import argparse
import json
import logging
import re
import sys
import threading
from collections import Counter
from pathlib import Path

from worker.storyline import head_to_head as storyline_head_to_head
from worker.storyline import score_line, slug
from worker.writer import card_facts, insight_facts

SERVER_NAME = "watershed-desk"
INSTRUCTIONS = "Read-only facts of the Watershed desk and of the league history. Every value is computed by code: cite each number with its field name."
DESK_TOOLS = ("get_moment", "get_match_state", "get_storyline")
HISTORY_TOOLS = ("player_history", "club_history", "head_to_head", "last_time")
LAST_TIME_KINDS = {"hat_trick": "a player scored three goals or more in a league match",
                   "hat_trick_by_substitute": "a substitute scored three goals or more after coming on",
                   "brace_by_substitute": "a substitute scored two goals or more after coming on",
                   "comeback_win_from_two_down": "a club won a league match after trailing by two goals or more"}
MAX_FACTS = 40  # per tool result, in insertion order
HISTORY_THROUGH = "last season of the league history; the current season is not in it"


def name_slug(name: str) -> str:
    """A player's name as part of a field name: "Marijn Scarpetta" gives marijn_scarpetta."""
    return re.sub(r"\W+", "_", name.lower()).strip("_")


def scalar(value) -> bool:
    """What a tool may return as a fact: a string or a number, never a bool, a list, a dict or None."""
    return isinstance(value, str) or (isinstance(value, (int, float)) and not isinstance(value, bool))


def tool_json(facts: dict, meanings: dict, subject: list) -> str:
    """A tool result: at most MAX_FACTS scalar facts, the meaning of each, and the clubs or players they are about."""
    kept = dict([(k, v) for k, v in facts.items() if scalar(v)][:MAX_FACTS])
    return json.dumps({"facts": kept, "field_meanings": {k: meanings[k] for k in kept if k in meanings}, "subject": [s for s in subject if isinstance(s, str)]},
                      ensure_ascii=False)


def error_json(message: str) -> str:
    """A tool's answer when it has no facts to give."""
    return json.dumps({"error": message})


class FactRegistry:
    """What the desk has written, as JSON snapshots the MCP tools read on AnyIO worker threads: one lock, immutable strings."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._moments: dict[str, str] = {}
        self._states: dict[str, str] = {}
        self._storylines: dict[str, str] = {}
        self.served: Counter = Counter()  # tool name -> calls served

    def put_moment(self, doc_id: str, facts: dict, meanings: dict) -> None:
        """The facts of a card or an insight, as FoundryWriter's get_moment returns them, byte for byte."""
        text = json.dumps({"facts": facts, "field_meanings": meanings}, ensure_ascii=False)
        with self._lock:
            self._moments[doc_id] = text

    def moment(self, doc_id: str) -> str | None:
        with self._lock:
            return self._moments.get(doc_id)

    def put_state(self, card_id: str, facts: dict, meanings: dict, subject: list) -> None:
        """The match as it stood at a card (`match_state_facts`)."""
        text = tool_json(facts, meanings, subject)
        with self._lock:
            self._states[card_id] = text

    def state(self, card_id: str) -> str | None:
        with self._lock:
            return self._states.get(card_id)

    def put_storyline(self, match_id: str, facts: dict, meanings: dict, subject: list) -> None:
        """The pre-match storyline of a match (`storyline_facts`)."""
        text = tool_json(facts, meanings, subject)
        with self._lock:
            self._storylines[match_id] = text

    def storyline(self, match_id: str) -> str | None:
        with self._lock:
            return self._storylines.get(match_id)

    def mark(self, tool: str) -> None:
        """Counts one call served by `tool`."""
        with self._lock:
            self.served[tool] += 1


# --- the match and the storyline ---------------------------------------------------------------------

def match_state_facts(cards: list[dict], card: dict, teams: dict[str, str]) -> tuple[dict, dict, list[str]]:
    """The match as it stood at `card`: the score from the goals that count (goals ruled out left out), the goal log, red cards and the largest deficit of each club."""
    def counted(moment: str) -> list[dict]:
        return sorted((c for c in cards if c.get("moment") == moment and c.get("status") != "retracted" and c.get("match_id") == card.get("match_id")
                       and c.get("clock_s", 0) <= card.get("clock_s", 0)), key=lambda c: (c.get("clock_s", 0), c.get("id", "")))

    goals, reds = counted("goal"), counted("red_card")
    score, deficit = {"home": 0, "away": 0}, {"home": 0, "away": 0}
    for g in goals:
        score[g["team"]] += 1
        for side, other in (("home", "away"), ("away", "home")):
            deficit[side] = max(deficit[side], score[other] - score[side])
    home, away = teams["home"], teams["away"]
    facts = {"match_minute": card["minute_label"], "match_score_now": f"{home} {score['home']}-{score['away']} {away}",
             "match_goal_log": ", ".join(f"{g['minute_label']} {g['player']['name']} ({g['team_name']})" for g in goals) or "no goal yet"}
    meanings = {"match_minute": "match minute these facts are computed at", "match_score_now": "score of the match at the moment, goals ruled out not counted",
                "match_goal_log": "goals that count so far, in order: minute, scorer, club"}
    for side, club in (("home", home), ("away", away)):
        s = slug(club)
        facts |= {f"match_{s}_goals": score[side], f"match_{s}_red_cards": sum(1 for r in reds if r.get("team") == side), f"match_{s}_largest_deficit": deficit[side]}
        meanings |= {f"match_{s}_goals": f"goals {club} have scored so far in this match", f"match_{s}_red_cards": f"players of {club} sent off so far in this match",
                     f"match_{s}_largest_deficit": f"the most goals {club} have been behind in this match so far (0: never behind)"}
    return facts, meanings, [home, away]


def storyline_facts(doc: dict) -> tuple[dict, dict, list[str]]:
    """The pre-match storyline as tool facts: each thread's angle in words, then the facts of its thread (prefixed story_).

    League points and stakes in percentage points are left out: "points" on air only ever means a change in a team's chance of winning, and a list is not a fact a line can quote.
    """
    facts: dict = {}
    meanings: dict = {}
    for i, t in enumerate(doc.get("threads") or [], start=1):
        facts[f"story_thread{i}_angle"] = t.get("angle")
        meanings[f"story_thread{i}_angle"] = f"what thread {i} of the pre-match storyline is about, in words ({t.get('kind')})"
        for k, v in (t.get("facts") or {}).items():
            if "points" in k.split("_") or k.endswith("_pts") or not scalar(v) or f"story_{k}" in facts:
                continue
            facts[f"story_{k}"] = v
            meanings[f"story_{k}"] = (t.get("field_meanings") or {}).get(k, k)
    if doc.get("history_through"):
        facts["history_through"] = doc["history_through"]
        meanings["history_through"] = HISTORY_THROUGH
    return facts, meanings, [doc.get("home"), doc.get("away")]


# --- the league history ---------------------------------------------------------------------------------

class HistoryFacts:
    """Read-only facts from the league history for the MCP tools: a player's career, a club, two clubs' meetings, the last time something happened. Pure functions of the history, cached under a lock (the tools run on worker threads)."""

    def __init__(self, history, view) -> None:
        self.history = history
        self.view = view  # a worker.insight_history.HistoryView, shared with the insight engine
        self._lock = threading.Lock()
        self._cache: dict = {}
        self._champions: list[dict] | None = None
        self._occurrences: dict[str, list] | None = None

    @classmethod
    def build(cls, history) -> "HistoryFacts":
        """Reads the history once, on the calling thread, before any server serves it: the matches (through the view) and the players."""
        from worker.insight_history import HistoryView

        view = HistoryView(history)
        ids = getattr(history, "ids", None)
        if ids:
            history.player_seasons(ids[0])
        return cls(history, view)

    @property
    def through(self) -> str:
        return self.view.seasons[-1]

    def _cached(self, key: tuple, compute):
        with self._lock:
            if key not in self._cache:
                self._cache[key] = compute()
            return self._cache[key]

    # --- a player -------------------------------------------------------------------------------

    def player(self, name: str, club: str | None = None) -> tuple[dict, dict, list[str]]:
        """A player's league career: goals, appearances (off the bench too), first season, clubs, last season and best season. Raises LookupError for an unknown name."""
        return self._cached(("player", name, club), lambda: self._player(name, club))

    def _player(self, name: str, club: str | None) -> tuple[dict, dict, list[str]]:
        h, labels, through = self.history, self.view.seasons, self.through
        ids = list(h.find_players(name))
        if club:
            ids = [i for i in ids if any(c["club"] == club for c in h.player(i)["clubs"])]
        if not ids:
            raise LookupError(f"unknown player {name}")

        def last_season(pid: str) -> int:
            return max((labels.index(c["season"]) for c in h.player(pid)["clubs"] if c["season"] in labels), default=-1)

        pid = min(ids, key=lambda i: (-last_season(i), i))
        p, rows = h.player(pid), h.player_seasons(pid)
        s = name_slug(name)
        first = p["clubs"][0]["season"] if p["clubs"] else self.view.first_season
        clubs = list(dict.fromkeys(c["club"] for c in p["clubs"]))
        goals_by_season: dict[str, int] = {}
        for r in rows:
            goals_by_season[r["season"]] = goals_by_season.get(r["season"], 0) + r["goals"]
        facts = {f"player_{s}_name": p["name"], f"player_{s}_league_goals": p["goals"], f"player_{s}_appearances": p["appearances"],
                 f"player_{s}_sub_appearances": p["sub_appearances"], f"player_{s}_first_season": first, f"player_{s}_clubs": ", ".join(clubs),
                 f"player_{s}_goals_last_season": goals_by_season.get(through, 0)}
        meanings = {f"player_{s}_name": "the player's name in the league history",
                    f"player_{s}_league_goals": f"league goals {name} scored in the history, top division, from {first} to {through}; the current season and tonight's match are not counted",
                    f"player_{s}_appearances": f"league appearances of {name} in the history, from {first} to {through}, those off the bench included; the current season is not counted",
                    f"player_{s}_sub_appearances": f"league appearances of {name} off the bench in the history, up to {through}",
                    f"player_{s}_first_season": f"first season of the career of {name} in the history",
                    f"player_{s}_clubs": f"clubs {name} played for in the history, in order",
                    f"player_{s}_goals_last_season": f"league goals {name} scored in {through}, the last season of the history"}
        if goals_by_season:
            best = max(goals_by_season, key=lambda season: (goals_by_season[season], labels.index(season) if season in labels else -1))
            if goals_by_season[best]:
                facts |= {f"player_{s}_best_season": best, f"player_{s}_best_season_goals": goals_by_season[best]}
                meanings |= {f"player_{s}_best_season": f"best scoring season of {name} in the history (the most recent if tied)",
                             f"player_{s}_best_season_goals": f"league goals {name} scored in {best}"}
        facts["history_through"] = through
        meanings["history_through"] = HISTORY_THROUGH
        return facts, meanings, [name, *clubs]

    # --- a club ---------------------------------------------------------------------------------

    def club(self, club: str) -> tuple[dict, dict, list[str]]:
        """A club's titles, seasons in the league, best finish, founding season and last title. Raises LookupError for an unknown club."""
        return self._cached(("club", club), lambda: self._club(club))

    def _club(self, club: str) -> tuple[dict, dict, list[str]]:
        c = self._club_record(club)
        labels, through, s = self.view.seasons, self.through, slug(club)
        facts = {f"club_{s}_titles": c["titles"], f"club_{s}_seasons_in_league": c["seasons_in_league"]}
        meanings = {f"club_{s}_titles": f"league titles {club} won in the history, from {self.view.first_season} to {through}",
                    f"club_{s}_seasons_in_league": f"seasons {club} played in the top division in the history, up to {through}"}
        if c.get("best_finish") is not None:
            facts[f"club_{s}_best_finish"] = c["best_finish"]
            meanings[f"club_{s}_best_finish"] = f"best league position of {club} in the history (1: champions)"
        facts[f"club_{s}_founded_season"] = c["founded_season"]
        meanings[f"club_{s}_founded_season"] = f"season {club} was founded"
        titles = [x["season"] for x in self._champions_list() if x["club"] == club and x["season"] in labels]
        if titles:
            facts |= {f"club_{s}_last_title_season": titles[-1], f"club_{s}_seasons_since_last_title": self.view.current_idx - labels.index(titles[-1])}
            meanings |= {f"club_{s}_last_title_season": f"season of the most recent league title of {club} in the history",
                         f"club_{s}_seasons_since_last_title": f"seasons between the last title of {club} and the current season"}
        facts["history_through"] = through
        meanings["history_through"] = HISTORY_THROUGH
        return facts, meanings, [club]

    def _club_record(self, club: str) -> dict:
        try:
            return self.history.club(club)
        except KeyError:
            raise LookupError(f"unknown club {club}") from None

    def _champions_list(self) -> list[dict]:
        if self._champions is None:
            self._champions = list(self.history.champions())
        return self._champions

    # --- two clubs ------------------------------------------------------------------------------

    def head_to_head(self, club_a: str, club_b: str) -> tuple[dict, dict, list[str]]:
        """Two clubs' league meetings: how many, wins of each, draws, first season, last meeting and biggest wins; the same text whatever the order of the clubs."""
        if club_a == club_b:
            raise LookupError("give two different clubs")
        a, b = sorted([club_a, club_b])
        return self._cached(("h2h", a, b), lambda: self._head_to_head(a, b))

    def _head_to_head(self, a: str, b: str) -> tuple[dict, dict, list[str]]:
        for club in (a, b):
            self._club_record(club)
        h = storyline_head_to_head(self.history, a, b)
        x, y = sorted([slug(a), slug(b)])
        p = f"h2h_{x}_{y}"
        facts: dict = {f"{p}_meetings": h["meetings"] if h else 0}
        meanings = {f"{p}_meetings": f"league meetings of {a} and {b} in the history, both venues, up to {self.through}"}
        if h:
            facts |= {f"{p}_draws": h["draws"], f"{p}_first_season": h["first_season"]}
            meanings |= {f"{p}_draws": f"meetings of {a} and {b} that ended in a draw", f"{p}_first_season": "season of their first meeting in the history"}
            for club in (a, b):
                facts[f"{p}_{slug(club)}_wins"] = h["wins"][club]
                meanings[f"{p}_{slug(club)}_wins"] = f"meetings won by {club}"
            last = h["last"][0]
            facts[f"{p}_last_meeting"] = f"{last['season']}: {score_line(last)}"
            meanings[f"{p}_last_meeting"] = "their most recent meeting in the history"
            for club in (a, b):
                m = h["biggest_win"].get(club)
                if m:
                    facts[f"{p}_biggest_{slug(club)}_win"] = f"{m['season']}: {score_line(m)}"
                    meanings[f"{p}_biggest_{slug(club)}_win"] = f"widest winning margin of {club} against the other club in the history (the most recent if tied)"
        facts["history_through"] = self.through
        meanings["history_through"] = HISTORY_THROUGH
        return facts, meanings, [a, b]

    # --- the last time --------------------------------------------------------------------------

    def last_time(self, kind: str, club: str | None = None) -> tuple[dict, dict, list[str]]:
        """The last time something happened in the history, and how rare it is in seasons; never as a number of matches."""
        if kind not in LAST_TIME_KINDS:
            raise LookupError(f"unknown kind {kind}")
        if club:
            self._club_record(club)
        return self._cached(("last", kind, club or None), lambda: self._last_time(kind, club or None))

    def occurrences(self) -> dict[str, list]:
        """Per kind, every occurrence in the history: (season index, match features, side, player id or None). Computed once."""
        if self._occurrences is None:
            occ: dict[str, list] = {k: [] for k in LAST_TIME_KINDS}
            for f in self.view.features:
                for side in ("home", "away"):
                    worst, stage = f.comeback(side)
                    if worst >= 2 and stage == 3:
                        occ["comeback_win_from_two_down"].append((f.season_idx, f, side, None))
                goals, off_bench = Counter(), Counter()
                for g in f.goals:
                    if g.own_goal or not g.player:
                        continue
                    goals[(g.player, g.side)] += 1
                    if g.player in f.sub_in and g.minute >= f.sub_in[g.player]:
                        off_bench[(g.player, g.side)] += 1
                for (pid, side), n in goals.items():
                    if n >= 3:
                        occ["hat_trick"].append((f.season_idx, f, side, pid))
                for (pid, side), n in off_bench.items():
                    if n >= 3:
                        occ["hat_trick_by_substitute"].append((f.season_idx, f, side, pid))
                    if n >= 2:
                        occ["brace_by_substitute"].append((f.season_idx, f, side, pid))
            self._occurrences = occ
        return self._occurrences

    def _last_time(self, kind: str, club: str | None) -> tuple[dict, dict, list[str]]:
        view = self.view
        found = [o for o in self.occurrences()[kind] if club is None or o[1].club(o[2]) == club]
        key = kind if club is None else f"{kind}_{slug(club)}"
        what = LAST_TIME_KINDS[kind] + (f" for {club}" if club else "")
        facts: dict = {f"{key}_count": len(found), f"{key}_seasons_with_one": len({o[0] for o in found}), "history_seasons": len(view.seasons),
                       "history_since": view.first_season, "history_through": self.through}
        meanings = {f"{key}_count": f"times {what} in the league history from {view.first_season} to {self.through}; the current season is not counted",
                    f"{key}_seasons_with_one": f"seasons of the history in which {what} at least once",
                    "history_seasons": "seasons in the league history", "history_since": "first season of the league history", "history_through": HISTORY_THROUGH}
        if found:
            season_idx, f, side, pid = max(found, key=lambda o: (o[0], o[1].index))
            facts |= {f"last_{key}_season": f.season, f"last_{key}_match": f"{f.home} {f.hg}-{f.ag} {f.away}", f"last_{key}_seasons_ago": view.current_idx - season_idx}
            meanings |= {f"last_{key}_season": f"season of the last time {what} in the history", f"last_{key}_match": "the match of that last time, with its final score",
                         f"last_{key}_seasons_ago": "seasons between that last time and the current season"}
            if club is None:
                facts[f"last_{key}_club"] = f.club(side)
                meanings[f"last_{key}_club"] = "the club of that last time"
            if pid:
                facts[f"last_{key}_player"] = view.player_name(pid)
                meanings[f"last_{key}_player"] = "the player of that last time"
        else:
            facts[f"last_{key}_season"] = "never in the history"
            meanings[f"last_{key}_season"] = f"it never happened in the history that {what}"
        return facts, meanings, [club] if club else []


# --- the server -----------------------------------------------------------------------------------------

def _answer(compute) -> str:
    try:
        facts, meanings, subject = compute()
    except LookupError as error:
        return error_json(str(error))
    return tool_json(facts, meanings, subject)


def build_server(source, history: HistoryFacts | None = None):
    """The MCP server over `source` (a FactRegistry, or a DeskDirSource for the stdio command): the desk's tools, and the history tools when `history` is given. Build it after the worker configures logging, since MCPServer calls logging.basicConfig."""
    from typing import Annotated, Literal

    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations
    from pydantic import Field

    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, log_level="WARNING")

    @server.tool(annotations=read_only, structured_output=False)
    def get_moment(card_id: str) -> str:
        """Return the facts of a card and the meaning of each field, as JSON."""
        source.mark("get_moment")
        return source.moment(card_id) or error_json(f"unknown card_id {card_id}")

    @server.tool(annotations=read_only, structured_output=False)
    def get_match_state(card_id: Annotated[str, Field(max_length=120)]) -> str:
        """The match as it stood at a card: the score, the goals that count in order (minute, scorer, club), red cards and the largest deficit of each club, with the meaning of each field, as JSON."""
        source.mark("get_match_state")
        return source.state(card_id) or error_json(f"unknown card_id {card_id}")

    @server.tool(annotations=read_only, structured_output=False)
    def get_storyline(match_id: Annotated[str, Field(max_length=120)]) -> str:
        """The pre-match storyline of a match: league positions, what is at stake (in %), form, head-to-head and players to watch, with the meaning of each field, as JSON."""
        source.mark("get_storyline")
        return source.storyline(match_id) or error_json(f"no storyline for match {match_id}")

    if history is None:
        return server

    @server.tool(annotations=read_only, structured_output=False)
    def player_history(name: Annotated[str, Field(max_length=80)], club: Annotated[str | None, Field(max_length=60)] = None) -> str:
        """A player's league career in the history, which stops before the current season: goals, appearances, appearances off the bench, first season, clubs and best season, as JSON. Give the name as written in the facts; club picks among namesakes."""
        source.mark("player_history")
        return _answer(lambda: history.player(name, club))

    @server.tool(annotations=read_only, structured_output=False)
    def club_history(club: Annotated[str, Field(max_length=60)]) -> str:
        """A club in the league history: titles, seasons in the league, best finish, founding season and last title, as JSON."""
        source.mark("club_history")
        return _answer(lambda: history.club(club))

    @server.tool(annotations=read_only, structured_output=False)
    def head_to_head(club_a: Annotated[str, Field(max_length=60)], club_b: Annotated[str, Field(max_length=60)]) -> str:
        """League meetings of two clubs in the history: how many, wins of each, draws, first season, last meeting and biggest wins, as JSON."""
        source.mark("head_to_head")
        return _answer(lambda: history.head_to_head(club_a, club_b))

    @server.tool(annotations=read_only, structured_output=False)
    def last_time(kind: Literal["hat_trick", "hat_trick_by_substitute", "brace_by_substitute", "comeback_win_from_two_down"],
                  club: Annotated[str | None, Field(max_length=60)] = None) -> str:
        """The last time something happened in the league history, and how rare it is in seasons, as JSON. kind: hat_trick, hat_trick_by_substitute, brace_by_substitute or comeback_win_from_two_down; club narrows it to one club."""
        source.mark("last_time")
        return _answer(lambda: history.last_time(kind, club))

    return server


# --- stdio, over a desk folder ----------------------------------------------------------------------------

class DeskDirSource:
    """The desk as the worker writes it with --desk-dir (cards.json, insights.json, storyline.json, state.json per match), read again at every call: what the stdio server serves."""

    def __init__(self, desk_dir) -> None:
        self.root = Path(desk_dir)

    def _read(self, match_id: str, name: str):
        """A file of a match folder; a match id that is not a folder right under the desk folder ("..", a path) reads nothing."""
        if match_id not in self._matches():
            return None
        path = self.root / match_id / name
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except (OSError, ValueError):
            return None

    def _matches(self) -> list[str]:
        return sorted(d.name for d in self.root.iterdir() if d.is_dir()) if self.root.is_dir() else []

    def _teams(self, match_id: str) -> dict[str, str]:
        state = self._read(match_id, "state.json") or {}
        if isinstance(state.get("teams"), dict):
            return state["teams"]
        story = self._read(match_id, "storyline.json") or {}
        return {"home": story.get("home") or "Home", "away": story.get("away") or "Away"}

    def _find_card(self, card_id: str) -> tuple[str, dict, list[dict]] | None:
        for match_id in self._matches():
            cards = self._read(match_id, "cards.json") or []
            for card in cards:
                if card.get("id") == card_id:
                    return match_id, card, cards
        return None

    def moment(self, doc_id: str) -> str | None:
        """The same JSON as FactRegistry.put_moment, for a stored card, insight or storyline."""
        for match_id in self._matches():
            teams = self._teams(match_id)
            docs = [("card", c) for c in self._read(match_id, "cards.json") or []] + [("insight", d) for d in self._read(match_id, "insights.json") or []]
            story = self._read(match_id, "storyline.json")
            if story:
                docs.append(("insight", story))
            for kind, doc in docs:
                if doc.get("id") == doc_id:
                    facts, meanings = card_facts(doc, teams) if kind == "card" else insight_facts(doc, teams)
                    return json.dumps({"facts": facts, "field_meanings": meanings}, ensure_ascii=False)
        return None

    def state(self, card_id: str) -> str | None:
        """The match as it stood at a stored card."""
        found = self._find_card(card_id)
        if found is None:
            return None
        match_id, card, cards = found
        return tool_json(*match_state_facts(cards, card, self._teams(match_id)))

    def storyline(self, match_id: str) -> str | None:
        """The stored storyline of a match."""
        story = self._read(match_id, "storyline.json")
        return tool_json(*storyline_facts(story)) if story else None

    def mark(self, tool: str) -> None:
        """Nothing to count on a folder."""


def main(argv: list[str] | None = None) -> None:
    """The stdio server over a desk folder, with the history tools when --history is given."""
    ap = argparse.ArgumentParser(description="Serve the desk's facts (and the league history) as a read-only MCP server on stdio.")
    ap.add_argument("--desk-dir", type=Path, required=True, help="the folder the worker writes with --desk-dir")
    ap.add_argument("--history", type=Path, help="league history (data/history/<seed>/): adds the history tools")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout carries the protocol
    history_facts = None
    if args.history:
        from league.history import History

        history_facts = HistoryFacts.build(History.load(args.history))
    build_server(DeskDirSource(args.desk_dir), history_facts).run("stdio")


if __name__ == "__main__":
    main()
