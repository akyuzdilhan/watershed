"""The storyline of a match: what makes it unique and what is at stake for each club and each player, before, during and after it.

Pre-match (`build_storyline`): table positions and points gaps, stakes as probabilities (chance now, if win, if draw, if loss) from the league's Monte Carlo, form over the last five matchdays, the head-to-head and players to watch from the fictional history, and three to five story threads. Every number sits in `facts` under an explicit field name with its meaning in `field_meanings`, as in `writer.card_facts`; a thread only carries facts of the storyline.

Live (`live_context`): what the current score means for the season if it holds, and what the next goal of either side would change. The rest of the season is played once per score with common random numbers (the same generator as `league.season.match_importance`), so a score never makes a club worse off than a worse score, and each score is computed once and cached.

Post-match (`post_match`): what the result changed: rank and chances before and after.

    python -m worker.storyline --league-seed 9 --matchday 30 [--history PATH] --out data/desk/demo-0007/storyline.json
"""

import argparse
import functools
import json
import weakref
from pathlib import Path
from typing import Any

import numpy as np

from league.clubs import DEMO_AWAY, DEMO_HOME
from league.model import N_CLUBS
from league.season import N_SIMS, RELEGATED, TOP, League, match_importance, outcome_probabilities, season_state
from league.table import ranks
from worker.writer import NB, verify

COMPETITION = "Synthetic League"
CURRENT_SEASON = "2026/27"  # the season of the demo, after the 50 seasons of the history (1976/77 to 2025/26)
EVENTS = ("title", "top4", "relegation")
EVENT_WORDS = {"title": "winning the title", "top4": "finishing in the top four", "relegation": "being relegated (18th to 20th)"}
ANGLE_WORDS = {"title": "winning the title", "top4": "a top-four finish", "relegation": "going down"}  # thread angles carry no number
ZONES = {"title": "first_place", "top4": "top4", "relegation": "drop_zone"}
DROP_RANK = N_CLUBS - RELEGATED + 1  # 18th
GOAL_MILESTONES, APPEARANCE_MILESTONES = 50, 100  # round numbers worth a mention: every 50 goals, every 100 appearances
GOALS_WITHIN_REACH, APPEARANCES_WITHIN_REACH = 3, 1
MAX_THREADS = 5


def slug(club: str) -> str:
    return club.lower().replace(" ", "_")


def pct(p: float) -> int:
    return int(round(100 * p))


def in_zone(event: str, rank: int) -> bool:
    return rank >= DROP_RANK if event == "relegation" else rank <= (TOP if event == "top4" else 1)


@functools.lru_cache(maxsize=8)
def league_for(seed: int, matchday: int) -> League:
    return League.create(seed, demo_matchday=matchday)


# --- the season --------------------------------------------------------------------------------------

_CHANCES: "weakref.WeakKeyDictionary[League, dict]" = weakref.WeakKeyDictionary()  # league -> (home, away, before, score, n_sims) -> club -> event -> chance


def chances(league: League, home: str, away: str, before: int, score: tuple[int, int] | None, n_sims: int = N_SIMS) -> dict[str, dict[str, float]]:
    """Both clubs' chances of the title, the top four and relegation, the rest of the season played `n_sims` times after `before` matchdays.

    `score` fixes the fixture home v away (None: it is simulated like the others). The generator is the one of `match_importance`, so 1-0 and 0-1 give exactly its `if_win` and `if_loss`, and every score sees the same other results: a better score never gives a worse chance.
    """
    cache = _CHANCES.setdefault(league, {})
    key = (home, away, before, score, n_sims)
    if key not in cache:
        h, a = league.index(home), league.index(away)
        fh, fa, hg, ag = league.simulate_rest(before, n_sims, league.rng(before, h, a))
        col = np.flatnonzero((fh == h) & (fa == a))
        if col.size != 1:
            raise ValueError(f"{home} v {away} is not among the fixtures left after matchday {before}")
        if score is not None:
            hg, ag = hg.copy(), ag.copy()
            hg[:, col], ag[:, col] = score
        probs = outcome_probabilities(league.final_ranks(before, fh, fa, hg, ag))
        cache[key] = {club: {e: float(probs[f"{e}_probability"][league.index(club)]) for e in EVENTS} for club in (home, away)}
    return cache[key]


def table_with(league: League, before: int, home: str, away: str, score: tuple[int, int]) -> dict[str, dict[str, int]]:
    """The table after `before` matchdays plus this one result (the other fixtures of the matchday not counted): rank and points per club."""
    t = league.table(before)
    pts, gd, gf = t["points"].copy(), t["goal_difference"].copy(), t["goals_for"].copy()
    h, a = league.index(home), league.index(away)
    x, y = score
    pts[h] += 3 if x > y else int(x == y)
    pts[a] += 3 if y > x else int(x == y)
    gd[h] += x - y
    gd[a] += y - x
    gf[h] += x
    gf[a] += y
    r = ranks(pts, gd, gf)
    return {club: {"rank": int(r[i]), "points": int(pts[i])} for i, club in enumerate(league.clubs)}


def form(league: League, club: str, matchday: int, n: int = 5) -> list[dict]:
    """The club's results over the `n` matchdays before `matchday`, oldest first."""
    i = league.index(club)
    out = []
    for md in range(max(1, matchday - n), matchday):
        day = league.schedule[md - 1]
        k = int(np.flatnonzero((day == i).any(axis=1))[0])
        home = int(day[k, 0]) == i
        gf, ga = int(league.home_goals[md - 1, k]), int(league.away_goals[md - 1, k])
        if not home:
            gf, ga = ga, gf
        opp = league.clubs[int(day[k, 1 if home else 0])]
        out.append({"matchday": md, "opponent": opp, "venue": "home" if home else "away", "goals_for": gf, "goals_against": ga,
                    "result": "W" if gf > ga else "D" if gf == ga else "L"})
    return out


def current_runs(league: League, club: str, matchday: int) -> tuple[int, int]:
    """Matches without a win and matches unbeaten, counted back from the last matchday before `matchday`."""
    results = [m["result"] for m in form(league, club, matchday, n=matchday - 1)]
    no_win = next((k for k, r in enumerate(reversed(results)) if r == "W"), len(results))
    unbeaten = next((k for k, r in enumerate(reversed(results)) if r == "L"), len(results))
    return no_win, unbeaten


def reverse_fixture(league: League, home: str, away: str, matchday: int) -> dict | None:
    """The other meeting of the two clubs this season, if it was played before `matchday`."""
    h, a = league.index(home), league.index(away)
    hit = np.argwhere((league.schedule[..., 0] == a) & (league.schedule[..., 1] == h))
    md, k = (int(v) for v in hit[0])
    if md + 1 >= matchday:
        return None
    return {"matchday": md + 1, "home": away, "away": home, "home_goals": int(league.home_goals[md, k]), "away_goals": int(league.away_goals[md, k])}


# --- the history ----------------------------------------------------------------------------------------

def score_line(m: dict) -> str:
    return f"{m['home']} {m['home_goals']}-{m['away_goals']} {m['away']}"


def head_to_head(history, home: str, away: str, last: int = 5) -> dict | None:
    meetings = history.head_to_head(home, away)
    if not meetings:
        return None
    wins = {home: 0, away: 0}
    biggest: dict[str, dict | None] = {home: None, away: None}
    draws = 0
    for m in meetings:
        if m["home_goals"] == m["away_goals"]:
            draws += 1
            continue
        winner = m["home"] if m["home_goals"] > m["away_goals"] else m["away"]
        wins[winner] += 1
        margin = abs(m["home_goals"] - m["away_goals"])
        best = biggest[winner]
        if best is None or margin > abs(best["home_goals"] - best["away_goals"]):  # newest first: a tie keeps the most recent
            biggest[winner] = m
    return {"meetings": len(meetings), "wins": wins, "draws": draws, "first_season": meetings[-1]["season"], "last": meetings[:last], "biggest_win": biggest}


def _scorers(m: dict) -> list[tuple[str, str]]:
    """(player id, club) of each goal of a history match. The record shape is the history's: `scorers` (or `goals`), each with `player_id` (or `id`) and `club` (or `team`, home or away); own goals are skipped."""
    out = []
    for s in m.get("scorers") or m.get("goals") or []:
        if not isinstance(s, dict) or s.get("own_goal"):
            continue
        pid = s.get("player_id") or s.get("id") or (s.get("player") or {}).get("id")
        club = s.get("club") or s.get("team")
        club = m["home"] if club == "home" else m["away"] if club == "away" else club
        if pid:
            out.append((pid, club))
    return out


def players_to_watch(history, club: str) -> list[dict]:
    """The club's top scorer of the last season of the history, and every scorer of that season within reach of a round number of goals or appearances."""
    season = history.seasons()[-1]
    goals: dict[str, int] = {}
    for m in history.matches(lambda m: m["season"] == season and club in (m["home"], m["away"])):
        for pid, c in _scorers(m):
            if c == club:
                goals[pid] = goals.get(pid, 0) + 1
    out = []
    for rank, (pid, n) in enumerate(sorted(goals.items(), key=lambda kv: (-kv[1], kv[0]))):
        p = history.player(pid)
        if not any(c["season"] == season and c["club"] == club for c in p.get("clubs", [])):
            continue  # scored for the club last season but left it
        milestone = None
        for kind, total, step, reach in (("goals", p.get("goals", 0), GOAL_MILESTONES, GOALS_WITHIN_REACH),
                                         ("appearances", p.get("appearances", 0), APPEARANCE_MILESTONES, APPEARANCES_WITHIN_REACH)):
            target = (total // step + 1) * step
            if target - total <= reach:
                milestone = {"kind": kind, "current": total, "target": target, "needed": target - total}
                break
        if rank == 0 or milestone:
            out.append({"id": pid, "name": p["name"], "position": p.get("position"), "club": club, "season": season, "season_goals": n,
                        "career_goals": p.get("goals", 0), "appearances": p.get("appearances", 0), "top_scorer": rank == 0, "milestone": milestone})
    return out


# --- pre-match --------------------------------------------------------------------------------------------

def build_storyline(seed: int = 9, matchday: int = 30, history=None, match_id: str = "demo-0007", home: str = DEMO_HOME, away: str = DEMO_AWAY,
                    n_sims: int = N_SIMS, league: League | None = None) -> dict:
    """The storyline document of the contract (`type: "storyline"`), written before kick-off."""
    league = league or league_for(seed, matchday)
    before = matchday - 1
    clubs = (home, away)
    rows = {r["club"]: r for r in season_state(league, before, n_sims)["table"]}
    stakes = match_importance(league, home, away, before, n_sims)
    now = chances(league, home, away, before, None, n_sims)
    draw = chances(league, home, away, before, (0, 0), n_sims)
    hs, as_ = slug(home), slug(away)
    facts: dict[str, Any] = {"competition": COMPETITION, "season": CURRENT_SEASON, "matchday": matchday, "fixture": f"{home} v {away}",
                             "score_before": f"{home} 0-0 {away}", "home_club": home, "away_club": away}
    meanings = {"competition": "league of the match", "season": "season of the match", "matchday": "matchday of the match, 1 to 38",
                "fixture": "home club v away club", "score_before": "score at kick-off", "home_club": "club playing at home", "away_club": "club playing away"}
    focus = {}
    for club in clubs:
        s, row, st = slug(club), rows[club], stakes[club]
        facts |= {f"{s}_rank": row["rank"], f"{s}_points": row["points"], f"{s}_played": row["played"], f"{s}_goal_difference": row["goal_difference"],
                  f"{s}_points_behind_leader": row["points_behind_leader"], f"{s}_points_clear_of_drop_zone": row["points_clear_of_drop_zone"],
                  f"{s}_points_if_win": row["points"] + 3, f"{s}_points_if_draw": row["points"] + 1}
        meanings |= {f"{s}_rank": f"{club}'s league position before kick-off, 1 to 20", f"{s}_points": f"{club}'s league points before kick-off",
                     f"{s}_played": f"matches {club} have played this season", f"{s}_goal_difference": f"{club}'s goal difference this season",
                     f"{s}_points_behind_leader": f"league points of the leader minus {club}'s",
                     f"{s}_points_clear_of_drop_zone": f"for a club ranked 1 to 17, its league points minus the 18th club's; otherwise its league points minus the 17th club's (zero or negative)",
                     f"{s}_points_if_win": f"{club}'s league points after a win", f"{s}_points_if_draw": f"{club}'s league points after a draw"}
        for e in EVENTS:
            facts |= {f"{s}_{e}_chance_pct": pct(now[club][e]), f"{s}_{e}_chance_if_win_pct": pct(st[f"{e}_if_win"]),
                      f"{s}_{e}_chance_if_draw_pct": pct(draw[club][e]), f"{s}_{e}_chance_if_loss_pct": pct(st[f"{e}_if_loss"]),
                      f"{s}_{e}_stake_pts": abs(pct(st[f"{e}_if_win"]) - pct(st[f"{e}_if_loss"]))}
            what = EVENT_WORDS[e]
            meanings |= {f"{s}_{e}_chance_pct": f"{club}'s chance of {what} at kick-off, Monte Carlo over the rest of the season, in %",
                         f"{s}_{e}_chance_if_win_pct": f"{club}'s chance of {what} if they win this match, in %",
                         f"{s}_{e}_chance_if_draw_pct": f"{club}'s chance of {what} if this match is drawn, in %",
                         f"{s}_{e}_chance_if_loss_pct": f"{club}'s chance of {what} if they lose this match, in %",
                         f"{s}_{e}_stake_pts": f"what this match is worth for {club}'s chance of {what}: the gap between the chances if they win and if they lose, in percentage points"}
        focus[club] = max(EVENTS, key=lambda e: facts[f"{s}_{e}_stake_pts"])
    for result, score in (("win", (1, 0)), ("draw", (0, 0)), ("loss", (0, 1))):
        t = table_with(league, before, home, away, score)
        for club in clubs:
            r = result if club == home or result == "draw" else {"win": "loss", "loss": "win"}[result]
            facts[f"{slug(club)}_rank_if_{r}"] = t[club]["rank"]
            meanings[f"{slug(club)}_rank_if_{r}"] = f"{club}'s league position after a {r}, this result added to the table at kick-off (the other matches of the matchday not counted)"
    facts["points_gap"] = abs(facts[f"{hs}_points"] - facts[f"{as_}_points"])
    facts["points_leader"] = home if facts[f"{hs}_points"] > facts[f"{as_}_points"] else away if facts[f"{as_}_points"] > facts[f"{hs}_points"] else "level"
    meanings |= {"points_gap": "league points between the two clubs before kick-off", "points_leader": "club with more league points before kick-off, or level"}

    recent = {club: form(league, club, matchday) for club in clubs}
    for club in clubs:
        s, f = slug(club), recent[club]
        no_win, unbeaten = current_runs(league, club, matchday)
        facts |= {f"{s}_form": "".join(m["result"] for m in f), f"{s}_form_points": sum(3 if m["result"] == "W" else m["result"] == "D" for m in f),
                  f"{s}_matches_without_win": no_win, f"{s}_matches_unbeaten": unbeaten}
        meanings |= {f"{s}_form": f"{club}'s results over the last {len(f)} matchdays, oldest first (W win, D draw, L loss)",
                     f"{s}_form_points": f"league points {club} took from the last {len(f)} matchdays",
                     f"{s}_matches_without_win": f"{club}'s current run of league matches without a win this season",
                     f"{s}_matches_unbeaten": f"{club}'s current run of league matches without a defeat this season"}
    rev = reverse_fixture(league, home, away, matchday)
    if rev:
        facts["reverse_fixture"] = f"matchday {rev['matchday']}: {score_line(rev)}"
        meanings["reverse_fixture"] = "the other meeting of the two clubs this season"

    h2h, watch, history_through = None, [], None
    if history is not None:
        history_through = history.seasons()[-1]
        facts["history_through"] = history_through
        meanings["history_through"] = f"last season of the league history; the history starts in {history.seasons()[0]}"
        for club in clubs:
            c, s = history.club(club), slug(club)
            facts |= {f"{s}_titles": c["titles"], f"{s}_seasons_in_league": c["seasons_in_league"], f"{s}_best_finish": c["best_finish"]}
            meanings |= {f"{s}_titles": f"league titles {club} have won in the history", f"{s}_seasons_in_league": f"seasons {club} have played in the league in the history",
                         f"{s}_best_finish": f"{club}'s best league position in the history"}
        h2h = head_to_head(history, home, away)
        if h2h:
            facts |= {"h2h_meetings": h2h["meetings"], f"h2h_{hs}_wins": h2h["wins"][home], "h2h_draws": h2h["draws"], f"h2h_{as_}_wins": h2h["wins"][away],
                      "h2h_first_season": h2h["first_season"], "h2h_last_meeting": f"{h2h['last'][0]['season']}: {score_line(h2h['last'][0])}",
                      "h2h_last_meetings": [f"{m['season']}: {score_line(m)}" for m in h2h["last"]]}
            meanings |= {"h2h_meetings": "league meetings of the two clubs in the history, both venues", f"h2h_{hs}_wins": f"meetings won by {home}",
                         "h2h_draws": "meetings drawn", f"h2h_{as_}_wins": f"meetings won by {away}", "h2h_first_season": "season of their first meeting in the history",
                         "h2h_last_meeting": "their most recent meeting in the history", "h2h_last_meetings": "their most recent meetings in the history, newest first"}
            for club in clubs:
                m = h2h["biggest_win"][club]
                if m:
                    facts[f"h2h_biggest_{slug(club)}_win"] = f"{m['season']}: {score_line(m)}"
                    meanings[f"h2h_biggest_{slug(club)}_win"] = f"{club}'s widest winning margin against the other club in the history (the most recent if tied)"
        for club in clubs:
            for k, p in enumerate(players_to_watch(history, club)):
                pre = f"{slug(club)}_player{k + 1}"
                p["fact_prefix"] = pre
                watch.append(p)
                facts |= {f"{pre}_name": p["name"], f"{pre}_goals_last_season": p["season_goals"], f"{pre}_career_goals": p["career_goals"],
                          f"{pre}_appearances": p["appearances"]}
                meanings |= {f"{pre}_name": f"a {club} player to watch" + (f" ({club}'s top scorer in {p['season']})" if p["top_scorer"] else ""),
                             f"{pre}_goals_last_season": f"league goals by {pre}_name for {club} in {p['season']}",
                             f"{pre}_career_goals": f"league goals by {pre}_name in the history, up to {history_through}",
                             f"{pre}_appearances": f"league appearances by {pre}_name in the history, up to {history_through}"}
                if p["milestone"]:
                    ms = p["milestone"]
                    facts |= {f"{pre}_milestone": f"{ms['target']} {ms['kind']}", f"{pre}_milestone_needed": ms["needed"]}
                    meanings |= {f"{pre}_milestone": f"the round number of career league {ms['kind']} {pre}_name is closest to",
                                 f"{pre}_milestone_needed": f"{ms['kind']} {pre}_name still needs to reach it, counted up to {history_through} (this season's matches are not in the history)"}

    doc = {
        "id": f"storyline-{match_id}", "type": "storyline", "match_id": match_id, "competition": COMPETITION, "season": CURRENT_SEASON,
        "matchday": matchday, "home": home, "away": away, "league": {"seed": league.seed, "matchday": matchday, "n_sims": n_sims},
        "history_through": history_through, "focus": focus, "facts": facts, "field_meanings": meanings,
        "form": recent, "head_to_head": h2h, "players_to_watch": watch,
    }
    doc["threads"] = threads(doc)
    doc |= pre_match_texts(doc)
    return doc


def _thread(doc: dict, key: str, kind: str, value: int, angle: str, fields: list[str]) -> dict:
    fields = [f for f in fields if f in doc["facts"]]
    return {"key": key, "kind": kind, "value": int(value), "angle": angle, "facts": {f: doc["facts"][f] for f in fields},
            "field_meanings": {f: doc["field_meanings"][f] for f in fields}}


def threads(doc: dict) -> list[dict]:
    """Three to five reasons why this match matters, the strongest first. `value` (0 to 100) ranks them; `angle` says it in words, with no number."""
    f, home, away = doc["facts"], doc["home"], doc["away"]
    hs, as_ = slug(home), slug(away)
    out = []
    ev = doc["focus"]
    if ev[home] == ev[away]:
        e = ev[home]
        both_in = [c for c in (home, away) if in_zone(e, f[f"{slug(c)}_rank"])]
        name = {"relegation": "relegation six-pointer", "top4": "top-four six-pointer", "title": "title decider"}[e]
        angle = f"A {name}: the result moves both clubs' chance of {ANGLE_WORDS[e]} more than any other stake" + (f"; {' and '.join(both_in)} start the day in the zone" if both_in else "")
        out.append(_thread(doc, f"{e}_six_pointer", "stakes", min(100, 50 + max(f[f"{hs}_{e}_stake_pts"], f[f"{as_}_{e}_stake_pts"])), angle,
                           [k for c in (hs, as_) for k in (f"{c}_rank", f"{c}_points", f"{c}_{e}_chance_pct", f"{c}_{e}_chance_if_win_pct",
                                                            f"{c}_{e}_chance_if_draw_pct", f"{c}_{e}_chance_if_loss_pct", f"{c}_{e}_stake_pts")]))
    else:
        for club in (home, away):
            s, e = slug(club), ev[club]
            out.append(_thread(doc, f"{s}_{e}_stake", "stakes", min(100, 40 + f[f"{s}_{e}_stake_pts"]), f"For {club}, this match is above all about {ANGLE_WORDS[e]}",
                               [f"{s}_rank", f"{s}_points", f"{s}_{e}_chance_pct", f"{s}_{e}_chance_if_win_pct", f"{s}_{e}_chance_if_draw_pct",
                                f"{s}_{e}_chance_if_loss_pct", f"{s}_{e}_stake_pts"]))
    crossing = [c for c in (home, away) if in_zone(ev[c], f[f"{slug(c)}_rank_if_win"]) != in_zone(ev[c], f[f"{slug(c)}_rank_if_loss"])]
    lines = sorted({ZONES[ev[c]].replace("_", " ") for c in crossing})
    gap = f["points_gap"]
    angle = (f"The line of the {' and the '.join(lines)} can move today: {' and '.join(crossing)} {'can swap sides' if len(crossing) == 2 else 'can cross it'} depending on the result" if crossing
             else "The table is tight: a win for the club behind would take it above the other" if gap <= 2 else "The gap in the table between the two clubs")
    out.append(_thread(doc, "table", "table", 45 if crossing else 30 if gap <= 2 else 20 if gap <= 5 else 10, angle,
                       ["points_gap", "points_leader", f"{hs}_rank", f"{as_}_rank", f"{hs}_points", f"{as_}_points",
                        f"{hs}_points_clear_of_drop_zone", f"{as_}_points_clear_of_drop_zone",
                        *[f"{c}_rank_if_{r}" for c in (hs, as_) for r in ("win", "draw", "loss")]]))
    diff = f[f"{hs}_form_points"] - f[f"{as_}_form_points"]
    runs = {(c, kind): f[f"{slug(c)}_matches_{kind}"] for c in (home, away) for kind in ("without_win", "unbeaten")}
    (hot, kind), run = max(runs.items(), key=lambda kv: kv[1])
    if abs(diff) >= 4:
        better, worse = (home, away) if diff > 0 else (away, home)
        angle = f"Form: {better} come in much stronger than {worse} over the last five matchdays"
    else:
        angle = f"Form: {hot} come in on {'a winless' if kind == 'without_win' else 'an unbeaten'} run"
    out.append(_thread(doc, "form", "streak", min(100, 6 * run + 3 * abs(diff)), angle,
                       [f"{hs}_form", f"{as_}_form", f"{hs}_form_points", f"{as_}_form_points", f"{hs}_matches_without_win", f"{as_}_matches_without_win",
                        f"{hs}_matches_unbeaten", f"{as_}_matches_unbeaten"]))
    if "reverse_fixture" in f:
        out.append(_thread(doc, "reverse_fixture", "storyline", 15, "They have already met this season", ["reverse_fixture"]))
    if "h2h_meetings" in f:
        out.append(_thread(doc, "head_to_head", "record", 30, "A long rivalry in the league history",
                           ["h2h_meetings", f"h2h_{hs}_wins", "h2h_draws", f"h2h_{as_}_wins", "h2h_first_season", "h2h_last_meeting",
                            f"h2h_biggest_{hs}_win", f"h2h_biggest_{as_}_win"]))
    for club in (home, away):
        s = slug(club)
        if f.get(f"{s}_titles") and (in_zone("relegation", f[f"{s}_rank"]) or f[f"{s}_relegation_chance_pct"] >= 10):
            out.append(_thread(doc, f"{s}_fallen_champions", "storyline", 35, f"{club}, champions in the history, are fighting to stay up",
                               [f"{s}_titles", f"{s}_best_finish", f"{s}_seasons_in_league", f"{s}_rank", f"{s}_relegation_chance_pct"]))
    for p in doc["players_to_watch"]:
        pre = p["fact_prefix"]
        if p["milestone"]:
            out.append(_thread(doc, f"{pre}_milestone", "milestone", 45 if p["milestone"]["needed"] == 1 else 30,
                               f"{p['name']} is within reach of a round number of career {p['milestone']['kind']}",
                               [f"{pre}_name", f"{pre}_milestone", f"{pre}_milestone_needed", f"{pre}_career_goals", f"{pre}_appearances"]))
        elif p["top_scorer"]:
            out.append(_thread(doc, f"{pre}_top_scorer", "player", 20 + min(20, p["season_goals"]), f"{p['name']} was {p['club']}'s top scorer last season",
                               [f"{pre}_name", f"{pre}_goals_last_season", f"{pre}_career_goals"]))
    out.sort(key=lambda t: -t["value"])  # stable: equal values keep the order above
    return out[:MAX_THREADS]  # stakes, table and form are always there: at least three


# --- pre-match card texts ---------------------------------------------------------------------------------

PRE_MATCH = {
    "en": {"headline": {"relegation": "{home} v {away}: a relegation battle", "top4": "{home} v {away}: a top-four battle", "title": "{home} v {away}: a title battle"},
           "why": {"relegation": "{club} are on {now}% to go down: {win}% with a win, {loss}% with a defeat.",
                   "top4": "{club} are on {now}% to finish in the top four: {win}% with a win, {loss}% with a defeat.",
                   "title": "{club} are on {now}% to win the title: {win}% with a win, {loss}% with a defeat."}},
    "fr": {"headline": {"relegation": "{home} - {away}, duel pour le maintien", "top4": "{home} - {away}, duel pour le haut de tableau", "title": "{home} - {away}, duel pour le titre"},
           "why": {"relegation": f"{{club}} a {{now}}{NB}% de risques de descendre{NB}: {{win}}{NB}% en cas de victoire, {{loss}}{NB}% en cas de défaite.",
                   "top4": f"{{club}} a {{now}}{NB}% de chances de finir parmi les quatre premiers{NB}: {{win}}{NB}% en cas de victoire, {{loss}}{NB}% en cas de défaite.",
                   "title": f"{{club}} a {{now}}{NB}% de chances d'être champion{NB}: {{win}}{NB}% en cas de victoire, {{loss}}{NB}% en cas de défaite."}},
    "es": {"headline": {"relegation": "{home}-{away}, duelo por la permanencia", "top4": "{home}-{away}, duelo por los puestos de cabeza", "title": "{home}-{away}, duelo por el título"},
           "why": {"relegation": "El {club} tiene un {now}% de opciones de descender: {win}% si gana, {loss}% si pierde.",
                   "top4": "El {club} tiene un {now}% de opciones de acabar entre los cuatro primeros: {win}% si gana, {loss}% si pierde.",
                   "title": "El {club} tiene un {now}% de opciones de ser campeón: {win}% si gana, {loss}% si pierde."}},
}


def pre_match_texts(doc: dict, languages: tuple[str, ...] = ("en", "fr", "es")) -> dict:
    """The pre-match card: the club with the most at stake, its chance now, if it wins and if it loses. Checked by the Verifier."""
    f = doc["facts"]
    club = max((doc["home"], doc["away"]), key=lambda c: f[f"{slug(c)}_{doc['focus'][c]}_stake_pts"])
    s, e = slug(club), doc["focus"][club]
    fields = {"now": f"{s}_{e}_chance_pct", "win": f"{s}_{e}_chance_if_win_pct", "loss": f"{s}_{e}_chance_if_loss_pct"}
    values = {k: f[v] for k, v in fields.items()} | {"club": club, "home": doc["home"], "away": doc["away"]}
    out = []
    for lang in languages:
        t = PRE_MATCH[lang]
        claims = [{"value": f[v], "source_field": v} for v in fields.values()]
        out.append({"headline": t["headline"][e].format(**values), "why": t["why"][e].format(**values), "lang": lang, "claims": claims})
    _, report = verify(json.dumps(out, ensure_ascii=False), list(languages), f)
    return {"texts": out, "writer": "template", "verification": {"verified": report["verified"], "attempts": 1}}


# --- live and post-match -----------------------------------------------------------------------------------

def _league_of(storyline: dict) -> League:
    lg = storyline["league"]
    return league_for(lg["seed"], lg["matchday"])


def _stakes_at(storyline: dict, score: tuple[int, int]) -> tuple[dict, dict]:
    lg, home, away = storyline["league"], storyline["home"], storyline["away"]
    league = _league_of(storyline)
    return (chances(league, home, away, lg["matchday"] - 1, score, lg["n_sims"]),
            table_with(league, lg["matchday"] - 1, home, away, score))


def _direction(before: int, after: int) -> str:
    return "up" if after > before else "down" if after < before else "unchanged"


def live_context(storyline: dict, match_state: dict) -> tuple[dict, dict]:
    """What the current score means for the season, for the Writer: (facts, field_meanings), to merge into `card_facts`.

    `match_state` is the contract's match state (`score: {home, away}`); a card can also pass the score after its goal or the score had the shot gone in. "If the score holds" counts this result only: the other fixtures of the matchday are not counted.
    """
    home, away = storyline["home"], storyline["away"]
    score = (int(match_state["score"]["home"]), int(match_state["score"]["away"]))
    sf, label = storyline["facts"], "final score" if match_state.get("final") else "if the score holds"
    holds, table = _stakes_at(storyline, score)
    nxt = {side: _stakes_at(storyline, (score[0] + (side == "home"), score[1] + (side == "away")))[0] for side in ("home", "away")}
    facts: dict[str, Any] = {"score_now": f"{home} {score[0]}-{score[1]} {away}"}
    meanings = {"score_now": "the score these facts are computed for"}
    for club in (home, away):
        s, e = slug(club), storyline["focus"][club]
        zone, what = ZONES[e], EVENT_WORDS[e]
        kick_rank, rank = sf[f"{s}_rank"], table[club]["rank"]
        kick, now = sf[f"{s}_{e}_chance_pct"], pct(holds[club][e])
        facts |= {f"{s}_rank_at_kickoff": kick_rank, f"{s}_rank_if_score_holds": rank, f"{s}_points_if_score_holds": table[club]["points"],
                  f"{s}_in_{zone}_at_kickoff": in_zone(e, kick_rank), f"{s}_in_{zone}_if_score_holds": in_zone(e, rank),
                  f"{s}_{e}_chance_at_kickoff_pct": kick, f"{s}_{e}_chance_if_score_holds_pct": now,
                  f"{s}_{e}_change_since_kickoff_pts": abs(now - kick), f"{s}_{e}_change_direction": _direction(kick, now)}
        meanings |= {f"{s}_rank_at_kickoff": f"{club}'s league position at kick-off", f"{s}_rank_if_score_holds": f"{club}'s league position {label}, this result added to the table at kick-off",
                     f"{s}_points_if_score_holds": f"{club}'s league points {label}",
                     f"{s}_in_{zone}_at_kickoff": f"whether {club} were in the {zone.replace('_', ' ')} at kick-off",
                     f"{s}_in_{zone}_if_score_holds": f"whether {club} are in the {zone.replace('_', ' ')} {label}",
                     f"{s}_{e}_chance_at_kickoff_pct": f"{club}'s chance of {what} at kick-off, in %",
                     f"{s}_{e}_chance_if_score_holds_pct": f"{club}'s chance of {what} {label}, in %",
                     f"{s}_{e}_change_since_kickoff_pts": f"how far {club}'s chance of {what} has moved since kick-off, in percentage points (direction in {s}_{e}_change_direction)",
                     f"{s}_{e}_change_direction": f"whether {club}'s chance of {what} is up, down or unchanged since kick-off"}
        if facts[f"{s}_in_{zone}_at_kickoff"] != facts[f"{s}_in_{zone}_if_score_holds"]:
            moved = f"{s}_{'leaves' if facts[f'{s}_in_{zone}_at_kickoff'] else 'enters'}_{zone}_if_score_holds"
            facts[moved] = True
            meanings[moved] = f"{club} {'climb out of' if e == 'relegation' and facts[f'{s}_in_{zone}_at_kickoff'] else 'drop into' if e == 'relegation' else 'leave' if facts[f'{s}_in_{zone}_at_kickoff'] else 'enter'} the {zone.replace('_', ' ')} {label}"
        if not match_state.get("final"):
            for side, scorer in (("home", home), ("away", away)):
                k = f"{s}_{e}_chance_if_{slug(scorer)}_score_next_pct"
                facts[k] = pct(nxt[side][club][e])
                meanings[k] = f"{club}'s chance of {what} if {scorer} score the next goal and the score then holds, in %"
    return facts, meanings


def post_match(storyline: dict, home_goals: int, away_goals: int) -> tuple[dict, dict]:
    """What the result changed, for the recap: (facts, field_meanings). Rank and chances before (kick-off) and after this result; the rank after the whole matchday as well."""
    home, away = storyline["home"], storyline["away"]
    lg, sf = storyline["league"], storyline["facts"]
    holds, table = _stakes_at(storyline, (home_goals, away_goals))
    full = _league_of(storyline).with_result(home, away, home_goals, away_goals).table(lg["matchday"])
    facts: dict[str, Any] = {"final_score": f"{home} {home_goals}-{away_goals} {away}"}
    meanings = {"final_score": "full-time score of the match"}
    for club in (home, away):
        s, e = slug(club), storyline["focus"][club]
        zone, what = ZONES[e], EVENT_WORDS[e]
        before, after = sf[f"{s}_{e}_chance_pct"], pct(holds[club][e])
        rank_full = int(full["rank"][_league_of(storyline).index(club)])
        facts |= {f"{s}_rank_before": sf[f"{s}_rank"], f"{s}_rank_after": table[club]["rank"], f"{s}_rank_after_matchday": rank_full,
                  f"{s}_points_before": sf[f"{s}_points"], f"{s}_points_after": table[club]["points"],
                  f"{s}_in_{zone}_before": in_zone(e, sf[f"{s}_rank"]), f"{s}_in_{zone}_after": in_zone(e, table[club]["rank"]),
                  f"{s}_{e}_chance_before_pct": before, f"{s}_{e}_chance_after_pct": after,
                  f"{s}_{e}_change_pts": abs(after - before), f"{s}_{e}_change_direction": _direction(before, after)}
        meanings |= {f"{s}_rank_before": f"{club}'s league position before the match", f"{s}_rank_after": f"{club}'s league position with this result added (other matches of the matchday not counted)",
                     f"{s}_rank_after_matchday": f"{club}'s league position after the whole matchday {lg['matchday']}",
                     f"{s}_points_before": f"{club}'s league points before the match", f"{s}_points_after": f"{club}'s league points after the match",
                     f"{s}_in_{zone}_before": f"whether {club} were in the {zone.replace('_', ' ')} before the match",
                     f"{s}_in_{zone}_after": f"whether {club} are in the {zone.replace('_', ' ')} with this result added",
                     f"{s}_{e}_chance_before_pct": f"{club}'s chance of {what} before the match, in %",
                     f"{s}_{e}_chance_after_pct": f"{club}'s chance of {what} after the match, in %",
                     f"{s}_{e}_change_pts": f"how far the result moved {club}'s chance of {what}, in percentage points (direction in {s}_{e}_change_direction)",
                     f"{s}_{e}_change_direction": f"whether the result moved {club}'s chance of {what} up or down"}
    return facts, meanings


# --- command line -------------------------------------------------------------------------------------------

def load_history(path: str | None):
    if path is None:
        return None
    from league.history import History  # built by the history lot; only its read API is used here
    return History.load(path) if hasattr(History, "load") else History(path)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Write the storyline of the demo match.")
    ap.add_argument("--league-seed", type=int, default=9)
    ap.add_argument("--matchday", type=int, default=30)
    ap.add_argument("--history", help="folder of the league history (data/history/<seed>)")
    ap.add_argument("--match-id", default="demo-0007")
    ap.add_argument("--sims", type=int, default=N_SIMS)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    doc = build_storyline(args.league_seed, args.matchday, load_history(args.history), args.match_id, n_sims=args.sims)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{out}: {len(doc['threads'])} threads ({', '.join(t['key'] for t in doc['threads'])}); pre-match card verified {doc['verification']['verified']}")
    print(doc["texts"][0]["headline"], "|", doc["texts"][0]["why"])


if __name__ == "__main__":
    main()
