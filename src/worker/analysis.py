"""The Analyst: two or three tactical notes on a goal or a big chance, for the producer and the commentators, shown in the console and never voiced.

The code measures the possession that ended in the shot on the 360 freeze frames of the feed (`analysis_facts`, with `worker.tactics`): where and how the ball was won, how far from goal, the passes and carries, how long it took, the pass that took out the most defenders, the shooter's space when the ball reached him, the xG, the height of the conceding team's back line in the 5 minutes before against its match average, and the share of the threat each team created in the 10 minutes before the shot against the 10 before that. Every fact comes with what it means, as the card facts do. The model only writes, and claims each number with its field; `verify_analysis` runs the Writer's Verifier on every line (claims, unclaimed numbers, units, style and on-air rules, names) plus the Analyst's own: never an offside margin as a number. One retry told what was rejected, then the template (`template_analysis`, the two strongest facts as two sentences per language).

`AnalystDesk` wraps the desk (`python -m worker ... --analyst template|foundry`, off by default): the card is written and stored as before, then the analysis is attached to it as `analysis` (docs/contracts.md) and audited. With a model, the analysis runs on a thread of its own (`AnalystRunner`) and is applied at the pipeline's next message, so it never delays a card; it has its own budget of model calls per match (`--analyst-calls`, 12). A goal VAR rules out keeps its analysis marked `retracted`: the console shows it as withdrawn. `--analyst-dry` runs the whole model path offline, with a scripted agent that answers with the template.
"""

import asyncio
import concurrent.futures
import copy
import json
import logging
import math
import queue
import re
import statistics
import threading
from typing import Any

from worker import tactics as T
from worker.duo import fr_de, fr_typography
from worker.writer import (EM_DASH, LANGUAGES, WRITER_INSTRUCTIONS, MACHINE_TRANSLATED, AgentWriter, check_numbers, model_attempts, names_ok, no_em_dash, normalise, numbers_in, style_issues,
                           word_count)

log = logging.getLogger(__name__)

FACTS_VERSION = 4  # 4: readings computed in code (move_tempo, space_vs_typical, line_vs_average, threat_share_reading, threat_trend), roles named; 3: no minute, no field named after a median or a window; 2: defenders taken out goal-side at the pass, behind the receiver at reception
ANALYSIS_MOMENTS = ("goal", "chance", "save")
QUICK_S, QUICK_PASSES, PATIENT_S = 15, 6, 40  # move_tempo: a quick counter, a patient build-up, else a direct move
SPACE_BAND, LINE_BAND_M, TREND_BAND_PTS = 0.15, 3, 10  # the bands of space_vs_typical, line_vs_average and threat_trend
MIN_PROGRESS_M = 10.0  # a pass that gains less ground takes nobody out
DEEP_REGAIN_X = 35.0  # won in the own third: a counter from deep
HIGH_REGAIN_X = 65.0  # won within 40 m of the opponent's goal: a high regain
LINE_WINDOW_S, THREAT_WINDOW_S = 300, 600
MIN_LINES, MAX_LINES, LINE_MAX_WORDS = 2, 3, 45
CARRIES = ("dribble", "take_on")
RESTART_HOW = {"throw_in": "throw_in", "goalkick": "goal_kick", "freekick_short": "free_kick", "freekick_crossed": "free_kick", "shot_freekick": "free_kick",
               "corner_short": "corner", "corner_crossed": "corner", "shot_penalty": "penalty", "keeper_claim": "keeper", "keeper_save": "keeper"}
WON_BY_PLAYER = ("tackle", "interception", "keeper")


def feed_actions(match) -> list[dict]:
    """The actions of a `worker.match.Match` in provider order, corrections applied and voided ones marked, as `web.matchfeed.build` gives them."""
    return [dict(a, voided=True) if a["event_id"] in match.voided else a for a in sorted(match.versions.values(), key=lambda a: a["seq"])]


def _regain_how(actions: list[dict], first: dict) -> str:
    kind = first.get("action_type", "")
    if kind in ("tackle", "interception"):
        return kind
    if kind in RESTART_HOW:
        return RESTART_HOW[kind]
    k = actions.index(first)
    prev = actions[k - 1] if k else None
    if prev is not None and prev["team"] != first["team"]:
        if prev.get("action_type") == "clearance":
            return "clearance"
        if prev.get("action_type") in T.PASS_TYPES and prev.get("result") != "success":
            return "misplaced_pass"
    return "loose_ball"


def defenders_taken_out(pass_action: dict, reception: dict) -> list[str]:
    """Outfield opponents goal-side of the ball when the pass was played and no longer goal-side of the receiver when it arrived (each in its own frame, the attacking team's), for a pass that gains `MIN_PROGRESS_M` or more; stricter than `tactics.defenders_bypassed`, which counts everyone standing between the two points."""
    if pass_action["end_x"] - pass_action["start_x"] < MIN_PROGRESS_M:
        return []
    goal_side = {p["player_id"] for p in T.opponents(pass_action) if p["location"][0] > pass_action["start_x"]}
    rx = T.actor_location(reception)[0]
    return sorted(p["player_id"] for p in T.opponents(reception) if p["player_id"] in goal_side and p["location"][0] < rx)


def move_tempo(duration_s: float, passes: int) -> str:
    if duration_s <= QUICK_S and passes <= QUICK_PASSES:
        return "quick counter"
    return "patient build-up" if duration_s >= PATIENT_S else "direct move"


def compare(a: float, b: float, words: tuple[str, str, str], ratio: float | None = None, margin: float | None = None) -> str:
    """words[0] when `a` is above `b` by more than `ratio` (relative) or by `margin` or more (absolute), words[1] when below, else words[2]."""
    if ratio is not None:
        return words[0] if a > b * (1 + ratio) else words[1] if a < b * (1 - ratio) else words[2]
    return words[0] if a - b >= margin else words[1] if b - a >= margin else words[2]


def _shares(actions: list[dict], side: str, start: float, end: float) -> int | None:
    """The `side` share (%) of the threat both teams created between `start` and `end` (match clock, s): completed passes and carries, forward value only."""
    mine = other = 0.0
    for a in actions:
        if start <= a["clock_s"] < end:
            v = max(T.threat_added(a), 0.0)
            if a["team"] == side:
                mine += v
            else:
                other += v
    return round(100 * mine / (mine + other)) if mine + other > 0 else None


def analysis_facts(feed: list[dict], card: dict, teams: dict[str, str] | None = None) -> tuple[dict, dict] | None:
    """The tactical facts of the possession that ended in the card's shot, and the meaning of each field; None when the shot is not in the feed or the card is not a goal or a chance. Only the actions before the shot are read, as the desk sees them live."""
    shot_id = card.get("shot_event_id") or card.get("event_id")
    if card.get("moment") not in ANALYSIS_MOMENTS or not any(a["event_id"] == shot_id for a in feed):
        return None
    move = T.chain(feed, shot_id)
    shot, first = move[-1], move[0]
    before = [a for a in feed if a["seq"] < shot["seq"]]
    side = shot["team"]
    opp = "away" if side == "home" else "home"
    names = dict(teams or {})
    for a in feed:
        names.setdefault(a["team"], a.get("team_name") or a["team"].title())
    team, opponent = names[side], names[opp]
    regain_x = T.actor_location(first)[0]
    how = _regain_how(feed, first)
    duration = round(shot["clock_s"] - first["clock_s"])
    passes = sum(1 for a in move[:-1] if a.get("action_type") in T.PASS_TYPES)
    facts: dict[str, Any] = {"moment": card["moment"], "team": team, "opponent": opponent,
                             "home_team": names["home"], "away_team": names["away"], "shooter_name": shot["player"]["name"],
                             "xg_pct": round(100 * shot["shot"]["xg"]) if shot.get("shot") else None,
                             "regain_how": how, "ball_winner_name": first["player"]["name"], "regain_from_own_goal_m": round(regain_x),
                             "regain_to_opponent_goal_m": round(T.LENGTH - regain_x), "high_regain": regain_x >= HIGH_REGAIN_X,
                             "passes_in_move": passes, "carries_in_move": sum(1 for a in move[:-1] if a.get("action_type") in CARRIES),
                             "players_in_move": len({a["player"]["id"] for a in move}), "move_duration_s": duration, "move_tempo": move_tempo(duration, passes)}
    meanings = {"moment": "what the card is about: goal, chance (a shot that missed) or save (a shot the keeper stopped)",
                "team": "the attacking team, whose possession ended in the shot", "opponent": "the defending team", "home_team": "home club of the match",
                "away_team": "away club of the match", "shooter_name": "the player who took the shot: the only player who may shoot, finish or score in a note",
                "xg_pct": "chance the shot had of being a goal (expected goals), in %",
                "regain_how": "how the attacking team won the ball at the start of the possession: tackle, interception, keeper (a catch), a restart (throw_in, goal_kick, free_kick, corner, penalty), clearance (picked up after an opponent's clearance), misplaced_pass (an opponent's pass that went astray) or loose_ball",
                "ball_winner_name": "the player of the attacking team who won the ball, or took the restart, at the start of the move",
                "regain_from_own_goal_m": "distance in metres from the attacking team's own goal line to where the ball was won",
                "regain_to_opponent_goal_m": "distance in metres from where the ball was won to the opponent's goal line",
                "high_regain": "true when the ball was won within 40 m of the opponent's goal",
                "passes_in_move": "passes the attacking team played from winning the ball to the shot", "carries_in_move": "carries and take-ons in the same possession",
                "players_in_move": "players of the attacking team who touched the ball in the possession, the shooter included",
                "move_duration_s": "seconds from winning the ball to the shot",
                "move_tempo": "what kind of move it was, computed from its seconds and passes: quick counter (15 s or less, 6 passes or fewer), patient build-up (40 s or more) or direct move; only a quick counter may be called quick, rapid or a counter"}
    breaks = [(len(defenders_taken_out(a, b)), math.hypot(a["end_x"] - a["start_x"], a["end_y"] - a["start_y"]), a, b) for a, b in T.receptions(move)]
    breaks = [t for t in breaks if t[0] > 0]
    if breaks:
        n, length, a, b = max(breaks, key=lambda t: (t[0], t[1]))
        facts |= {"key_pass_defenders_taken_out": n, "key_passer_name": a["player"]["name"], "key_pass_receiver_name": b["player"]["name"], "key_pass_length_m": round(length)}
        meanings |= {"key_pass_defenders_taken_out": "most outfield opponents one pass of the move took out: goal-side of the ball when it was played, behind the receiver when it arrived; only passes gaining 10 m or more",
                     "key_passer_name": "the player who played that key pass (not the shooter, unless the names are the same)",
                     "key_pass_receiver_name": "the player who received the key pass", "key_pass_length_m": "length of the key pass in metres"}
    spaces = T.space_at_reception(before + [shot])
    shooter_id = shot["player"]["id"]
    mine = [s for s in spaces if s["event_id"] == shot_id or (s["receiver_id"] == shooter_id and s["event_id"] in {a["event_id"] for a in move})]
    if mine:
        facts["shooter_space_m"] = round(mine[-1]["space_m"], 1)
        meanings["shooter_space_m"] = "distance in metres from the shooter to the nearest opponent (keeper included) when the ball reached him in this move"
        if spaces:
            facts["typical_space_m"] = round(statistics.median(s["space_m"] for s in spaces), 1)
            facts["space_vs_typical"] = compare(facts["shooter_space_m"], facts["typical_space_m"], ratio=SPACE_BAND, words=("more room than usual", "less room than usual", "about usual"))
            meanings["typical_space_m"] = "the space a player usually had when receiving the ball in this match so far, both teams (half the receptions had less)"
            meanings["space_vs_typical"] = "the shooter's space against the usual one, computed: more room than usual, less room than usual or about usual (within 15%); say it this way, never the other way round"
    heights = [r for r in T.line_heights(before) if r["in_possession"] == side]
    recent = [r[opp] for r in heights if shot["clock_s"] - LINE_WINDOW_S <= r["clock_s"] < shot["clock_s"]]
    if recent and len(heights) > len(recent):
        last, usual = round(statistics.median(recent)), round(statistics.median(r[opp] for r in heights))
        facts |= {"opponent_line_before_shot_m": last, "opponent_line_usual_m": usual, "opponent_line_gap_m": abs(last - usual),
                  "line_vs_average": compare(last, usual, margin=LINE_BAND_M, words=("higher than usual", "deeper than usual", "as usual"))}
        meanings |= {"opponent_line_before_shot_m": "how far in metres the defending team's last outfield defender usually stood from its own goal line while the attacking team had the ball, in the 5 minutes before the shot",
                     "opponent_line_usual_m": "the same over the whole match so far, \"on average\"", "opponent_line_gap_m": "difference between the two, in metres",
                     "line_vs_average": "how the defending team's line sat before the shot against its average, computed: higher than usual, deeper than usual or as usual (within 3 m)"}
    t = shot["clock_s"]
    last_share = _shares(before, side, t - THREAT_WINDOW_S, t)
    prev_share = _shares(before, side, t - 2 * THREAT_WINDOW_S, t - THREAT_WINDOW_S)
    if last_share is not None and prev_share is not None and t >= 2 * THREAT_WINDOW_S:
        facts |= {"team_threat_share_before_shot_pct": last_share, "team_threat_share_earlier_pct": prev_share, "team_threat_share_shift_pts": abs(last_share - prev_share),
                  "threat_share_reading": "in control" if last_share >= 55 else "under the cosh" if last_share <= 45 else "even",
                  "threat_trend": compare(last_share, prev_share, margin=TREND_BAND_PTS - 0.5, words=("rising", "falling", "steady"))}
        meanings |= {"team_threat_share_before_shot_pct": "attacking team's share of the threat both teams created in the ten minutes before the shot (say \"in the last ten minutes\", in words), in %; threat is the gain in shot value (xG of a shot from there) of completed passes and carries",
                     "team_threat_share_earlier_pct": "the same share in the ten minutes before that, in %",
                     "team_threat_share_shift_pts": "difference between the two shares, in percentage points",
                     "threat_share_reading": "what that share means, computed: in control (55% or more), even (between 45% and 55%) or under the cosh (45% or less); a team under the cosh never had the advantage",
                     "threat_trend": "how the share moved from the ten minutes before, computed: rising or falling (by 10 points or more) or steady"}
    facts = {k: v for k, v in facts.items() if v is not None}
    return facts, {k: v for k, v in meanings.items() if k in facts}


# --- template -------------------------------------------------------------------------------------
# Each note is used only when its condition holds; the two with the highest score are kept, in that order. Placeholders are fact fields
# (numbers claimed) or derived words: {how} (how the ball was won, naming who won it), {de_*} (French "de" before a name).

HOW = {
    "en": {"tackle": "{ball_winner_name}'s tackle", "interception": "{ball_winner_name}'s interception", "keeper": "{ball_winner_name}'s catch",
           "throw_in": "a throw-in by {ball_winner_name}", "goal_kick": "a goal kick by {ball_winner_name}", "free_kick": "a free kick by {ball_winner_name}",
           "corner": "a corner by {ball_winner_name}", "penalty": "a penalty by {ball_winner_name}", "clearance": "{ball_winner_name} picking up a cleared ball",
           "misplaced_pass": "{ball_winner_name} picking up a stray pass", "loose_ball": "{ball_winner_name} picking up a loose ball"},
    "fr": {"tackle": "le tacle {de_ball_winner_name}", "interception": "l'interception {de_ball_winner_name}", "keeper": "la prise de balle {de_ball_winner_name}",
           "throw_in": "une touche {de_ball_winner_name}", "goal_kick": "un six mètres {de_ball_winner_name}", "free_kick": "un coup franc {de_ball_winner_name}",
           "corner": "un corner {de_ball_winner_name}", "penalty": "un penalty {de_ball_winner_name}", "clearance": "un ballon repoussé, récupéré par {ball_winner_name}",
           "misplaced_pass": "une passe adverse ratée, récupérée par {ball_winner_name}", "loose_ball": "un ballon qui traîne, récupéré par {ball_winner_name}"},
    "es": {"tackle": "la entrada de {ball_winner_name}", "interception": "la intercepción de {ball_winner_name}", "keeper": "un balón atrapado por {ball_winner_name}",
           "throw_in": "un saque de banda de {ball_winner_name}", "goal_kick": "un saque de puerta de {ball_winner_name}", "free_kick": "una falta sacada por {ball_winner_name}",
           "corner": "un córner de {ball_winner_name}", "penalty": "un penalti de {ball_winner_name}", "clearance": "un despeje rival recogido por {ball_winner_name}",
           "misplaced_pass": "un pase rival errado, recogido por {ball_winner_name}", "loose_ball": "un balón suelto recogido por {ball_winner_name}"},
}

NOTES = {  # one sentence each, at most 2 numbers, the reading computed in code (move_tempo, space_vs_typical, line_vs_average, threat_share_reading, threat_trend)
    "high_regain": {"en": "{team} won it high, {how} just {regain_to_opponent_goal_m} m from goal, and the shot came {move_duration_s} s later before the defence could reset.",
                    "fr": "{team} récupère haut, avec {how} à {regain_to_opponent_goal_m} m du but adverse, et le tir part {move_duration_s} s plus tard, avant que la défense ne se replace.",
                    "es": "El {team} recupera arriba, con {how} a {regain_to_opponent_goal_m} m de la portería rival, y el tiro llega {move_duration_s} s después, antes de que la defensa se reorganice."},
    "counter": {"en": "A quick counter from deep: {how} {regain_from_own_goal_m} m from the {team} goal, and {move_duration_s} s later the shot was away.",
                "fr": "Un contre éclair parti de loin : {how} à {regain_from_own_goal_m} m de son propre but, et {move_duration_s} s plus tard le tir est parti.",
                "es": "Un contragolpe rápido desde atrás: {how} a {regain_from_own_goal_m} m de su propia portería, y {move_duration_s} s después llega el tiro."},
    "quick": {"en": "A quick counter: {how} {regain_to_opponent_goal_m} m from goal, and {move_duration_s} s later the shot was away.",
              "fr": "Un contre éclair : {how} à {regain_to_opponent_goal_m} m du but adverse, et {move_duration_s} s plus tard le tir est parti.",
              "es": "Un contragolpe rápido: {how} a {regain_to_opponent_goal_m} m de la portería rival, y {move_duration_s} s después llega el tiro."},
    "direct": {"en": "It began with {how} {regain_from_own_goal_m} m from the {team} goal, and a direct move took {move_duration_s} s to reach the shot.",
               "fr": "Tout commence par {how} à {regain_from_own_goal_m} m de son propre but, et une action directe mène au tir en {move_duration_s} s.",
               "es": "Todo empieza con {how} a {regain_from_own_goal_m} m de su propia portería, y una jugada directa llega al tiro en {move_duration_s} s."},
    "build_up": {"en": "{team} built patiently, keeping the ball for {move_duration_s} s and {passes_in_move} passes before the shot.",
                 "fr": "{team} construit patiemment, en gardant le ballon {move_duration_s} s et {passes_in_move} passes avant le tir.",
                 "es": "El {team} construye con paciencia, con el balón durante {move_duration_s} s y {passes_in_move} pases antes del tiro."},
    "line_break": {"en": "{key_passer_name}'s pass took {key_pass_defenders_taken_out} {opponent} defenders out of the game and found {key_pass_receiver_name}, the ball that opened them up.",
                   "fr": "La passe {de_key_passer_name} élimine d'un coup {key_pass_defenders_taken_out} défenseurs adverses et trouve {key_pass_receiver_name} : c'est le ballon qui ouvre la défense.",
                   "es": "El pase de {key_passer_name} deja atrás a {key_pass_defenders_taken_out} defensores del {opponent} y encuentra a {key_pass_receiver_name}: es el balón que abre la defensa."},
    "space": {"en": "{shooter_name} had {shooter_space_m} m of space when the ball arrived, more room than usual in this match, and nobody was close enough to block.",
              "fr": "{shooter_name} reçoit avec {shooter_space_m} m d'espace, plus que d'habitude dans ce match, et personne n'est assez près pour contrer.",
              "es": "{shooter_name} recibe con {shooter_space_m} m de espacio, más de lo habitual en este partido, y nadie llega a tiempo para tapar el tiro."},
    "line_higher": {"en": "{opponent} had pushed their back line up before the shot, their last defender {opponent_line_before_shot_m} m from goal against {opponent_line_usual_m} m on average, leaving space in behind.",
                    "fr": "{opponent} avait remonté sa ligne avant le tir, son dernier défenseur à {opponent_line_before_shot_m} m de son but contre {opponent_line_usual_m} m en moyenne, et l'espace était dans le dos.",
                    "es": "El {opponent} había adelantado su línea antes del tiro, con su último defensor a {opponent_line_before_shot_m} m de su portería frente a {opponent_line_usual_m} m de media, y dejó espacio a la espalda."},
    "line_deeper": {"en": "{opponent} had dropped deep before the shot, their last defender {opponent_line_before_shot_m} m from goal against {opponent_line_usual_m} m on average, and the pressure told.",
                    "fr": "{opponent} avait reculé avant le tir, son dernier défenseur à {opponent_line_before_shot_m} m de son but contre {opponent_line_usual_m} m en moyenne, et la pression a fini par payer.",
                    "es": "El {opponent} se había hundido antes del tiro, con su último defensor a {opponent_line_before_shot_m} m de su portería frente a {opponent_line_usual_m} m de media, y la presión acabó pagando."},
    "threat_up": {"en": "{team} were in control in the last ten minutes, with {team_threat_share_before_shot_pct}% of the threat after {team_threat_share_earlier_pct}% before, so this had been coming.",
                  "fr": "{team} avait la main sur les dix dernières minutes, avec {team_threat_share_before_shot_pct} % de la menace après {team_threat_share_earlier_pct} % auparavant : ce but se préparait.",
                  "es": "El {team} mandaba en los últimos diez minutos, con el {team_threat_share_before_shot_pct}% de la amenaza tras el {team_threat_share_earlier_pct}% anterior: este gol se veía venir."},
    "threat_down": {"en": "Against the run of play: {team} had only {team_threat_share_before_shot_pct}% of the threat in the last ten minutes, after {team_threat_share_earlier_pct}% before.",
                    "fr": "Contre le cours du jeu : {team} n'avait que {team_threat_share_before_shot_pct} % de la menace sur les dix dernières minutes, après {team_threat_share_earlier_pct} % auparavant.",
                    "es": "Contra la corriente del partido: el {team} solo tenía el {team_threat_share_before_shot_pct}% de la amenaza en los últimos diez minutos, tras el {team_threat_share_earlier_pct}% anterior."},
    "move": {"en": "A move of {passes_in_move} passes, finished from a {xg_pct}% chance (expected goals).",
             "fr": "Une action de {passes_in_move} passes, conclue sur une occasion à {xg_pct} % de but (expected goals).",
             "es": "Una jugada de {passes_in_move} pases, cerrada con una ocasión del {xg_pct}% de gol esperado."},
    "xg": {"en": "{shooter_name}'s shot was a {xg_pct}% chance (expected goals).", "fr": "Le tir {de_shooter_name} était une occasion à {xg_pct} % de but (expected goals).",
           "es": "El tiro de {shooter_name} era una ocasión del {xg_pct}% de gol esperado."},
}
NOTES = {key: {lang: fr_typography(v) if lang == "fr" else v for lang, v in by_lang.items()} for key, by_lang in NOTES.items()}
HOW["fr"] = fr_typography(HOW["fr"])


def note_scores(facts: dict) -> list[tuple[int, str]]:
    """(score, note key) of every note true for the facts, strongest first; ties keep the order of NOTES. Each note follows the reading computed in code."""
    f = facts.get
    out = []
    tempo = f("move_tempo")
    if f("regain_how") in HOW["en"] and f("passes_in_move", 0) >= 2:
        if f("high_regain") and f("regain_how") in WON_BY_PLAYER + ("misplaced_pass", "loose_ball") and tempo != "patient build-up":
            out.append((90, "high_regain"))
        elif tempo == "quick counter":
            out.append((75, "counter") if f("regain_from_own_goal_m", 99) < DEEP_REGAIN_X else (60, "quick"))
        elif tempo == "patient build-up":
            out.append((45, "build_up"))
        else:
            out.append((40, "direct"))
    if f("key_pass_defenders_taken_out", 0) >= 3:
        out.append((45 + 10 * facts["key_pass_defenders_taken_out"], "line_break"))
    if f("shooter_space_m", 0) >= 3 and f("space_vs_typical") == "more room than usual":
        out.append((round(55 + 3 * facts["shooter_space_m"]), "space"))
    if f("opponent_line_gap_m", 0) >= 4 and f("line_vs_average") in ("higher than usual", "deeper than usual"):
        out.append((45 + 3 * facts["opponent_line_gap_m"], "line_higher" if facts["line_vs_average"] == "higher than usual" else "line_deeper"))
    if f("team_threat_share_shift_pts", 0) >= 15:
        if f("threat_share_reading") == "in control" and f("threat_trend") == "rising":
            out.append((45 + facts["team_threat_share_shift_pts"], "threat_up"))
        elif f("threat_share_reading") == "under the cosh" and f("threat_trend") == "falling":
            out.append((45 + facts["team_threat_share_shift_pts"], "threat_down"))
    if f("passes_in_move", 0) >= 2 and "xg_pct" in facts:
        out.append((20, "move"))
    if "xg_pct" in facts:
        out.append((10, "xg"))
    order = list(NOTES)
    return sorted(out, key=lambda t: (-t[0], order.index(t[1])))


def _number(v: Any, lang: str) -> str:
    if isinstance(v, float):
        s = f"{v:.1f}"
        return s.replace(".", ",") if lang in ("fr", "es") else s
    return str(v)


def fill_note(template: str, facts: dict, lang: str) -> tuple[str, list[dict]] | None:
    """A note with its placeholders filled from the facts, and a claim for each number; None when a field is missing."""
    claims, values = [], {}
    names = re.findall(r"\{(\w+)\}", template)
    if "how" in names:
        how = HOW[lang].get(str(facts.get("regain_how")))
        if how is None:
            return None
        template = template.replace("{how}", how)
        names = re.findall(r"\{(\w+)\}", template)
    for n in names:
        key = n[3:] if n.startswith("de_") else n
        v = facts.get(key)
        if v is None or isinstance(v, bool):
            return None
        if n.startswith("de_"):
            values[n] = fr_de(str(v))
        elif isinstance(v, (int, float)):
            values[n] = _number(v, lang)
            if not any(c["source_field"] == key for c in claims):
                claims.append({"value": v, "source_field": key})
        else:
            values[n] = str(v)
    return template.format(**values), claims


def template_analysis(facts: dict, languages: list[str] = list(LANGUAGES), count: int = MIN_LINES) -> dict:
    """`{lang: [{"line", "claims"}]}`: the `count` strongest notes true for the facts, the same ones in every language."""
    keys = [k for _, k in note_scores(facts)]
    out = {}
    for lang in languages:
        lines = []
        for key in keys:
            filled = fill_note(NOTES[key][lang], facts, lang)
            if filled is not None:
                lines.append({"line": filled[0], "claims": filled[1]})
            if len(lines) == count:
                break
        out[lang] = lines
    return out


# --- Verifier -------------------------------------------------------------------------------------

OFFSIDE_WORDS = {"en": r"(?i)\boffside\b", "fr": r"(?i)\bhors-jeu\b", "es": r"(?i)\bfuera de juego\b"}
MARGIN = r"(?i)\d+(?:[.,]\d+)?\s?(?:cm|centim\w*|mm)\b"  # a distance in centimetres is only ever an offside margin


MAX_NUMBERS_PER_SENTENCE = 2
DATA_DUMP = r"\([^)]*\d[^)]*\)"  # "(34 m recent, 31 m match)": numbers in brackets read as a table, not as a pundit
FIELD_WORDS = {"en": r"(?i)\bmedians?\b|\brecent\b|\bchange\s+\d|\b[a-z]+_[a-z_]+\b",  # the words of the field names
               "fr": r"(?i)\bm[ée]dianes?\b|\bchange\s+\d|\b[a-z]+_[a-z_]+\b", "es": r"(?i)\bmedianas?\b|\bchange\s+\d|\b[a-z]+_[a-z_]+\b"}


def analysis_issues(line: str, lang: str) -> list[dict]:
    """The Analyst's own rules: an offside is "by a whisker" or "clear", never a margin; no centimetres at all; no numbers in brackets, no field-like word (median, recent, "change 3", a_field_name), at most `MAX_NUMBERS_PER_SENTENCE` numbers in a sentence; no em dash."""
    found = [{"rule": "offside_margin", "text": m.group(0)} for m in re.finditer(MARGIN, line)]
    found += [{"rule": "data_dump", "text": m.group(0)} for m in re.finditer(DATA_DUMP, line)]
    found += [{"rule": "field_word", "text": m.group(0)} for m in re.finditer(FIELD_WORDS.get(lang, FIELD_WORDS["en"]), line)]
    for s in re.split(r"(?<=[.!?])\s+", line):
        if lang in OFFSIDE_WORDS and re.search(OFFSIDE_WORDS[lang], s) and re.search(r"\d", s):
            found.append({"rule": "offside_margin", "text": s})
        if len(numbers_in(s)) > MAX_NUMBERS_PER_SENTENCE:
            found.append({"rule": "too_many_numbers", "text": s})
    if EM_DASH in line:
        found.append({"rule": "em_dash", "text": EM_DASH})
    return found


MORE_WORDS = {"en": r"(?i)\b(?:above|more|bigger|larger|higher|greater|over|up from)\b", "fr": r"(?i)\b(?:plus|au-dessus|supérieure?s?)\b",
              "es": r"(?i)\b(?:más|por encima|mayor(?:es)?|superior(?:es)?)\b"}
LESS_WORDS = {"en": r"(?i)\b(?:below|less|smaller|lower|deeper|fewer|under|down from)\b", "fr": r"(?i)\b(?:moins|en dessous|en deçà|inférieure?s?)\b",
              "es": r"(?i)\b(?:menos|por debajo|menor(?:es)?|inferior(?:es)?)\b"}
PAIRS = (("shooter_space_m", "typical_space_m"), ("opponent_line_before_shot_m", "opponent_line_usual_m"), ("team_threat_share_before_shot_pct", "team_threat_share_earlier_pct"))
CONTROL_WORDS = {"en": r"(?i)\b(?:in control|on top|dominat\w*|the advantage|took over|taken over|in charge|controll\w*)\b",
                 "fr": r"(?i)(?:\bla main\b|\bl['’]ascendant\b|\bdomin\w*|\bmaîtris\w*|\bcontrôl\w*)", "es": r"(?i)\b(?:mandaba|mandar|domin\w*|control\w*|adueñ\w*|el mando)\b"}
RUN_OF_PLAY_WORDS = {"en": r"(?i)\b(?:run of play|under the cosh|pinned back|on the back foot)\b", "fr": r"(?i)\b(?:cours du jeu|subissait|dos au mur)\b",
                     "es": r"(?i)\b(?:corriente del partido|sufría|encerrad\w*)\b"}
FINISH_WORDS = {"en": r"(?i)\b(?:finish(?:es|ed|ing)?|shoot(?:s|ing)?|scor(?:e|es|ed|ing)|strik(?:e|es|ing)|struck|slot(?:s|ted)?|fired)\b",
                "fr": r"(?i)\b(?:frapp(?:e|er|é|ait)|tir(?:e|er|é)|marqu(?:e|er|é)|conclu(?:t|re))\b", "es": r"(?i)\b(?:remat(?:a|ar|ó|e)|dispar(?:a|ar|ó)|marc(?:a|ar|ó)|defin(?:e|ir|ió))\b"}
TEMPO_WORDS = {"en": r"(?i)\b(?:quick|quickly|rapid|fast|counter|counters|counter-attack\w*|counterattack\w*|breakaway)\b",
               "fr": r"(?i)\b(?:rapides?|rapidement|vite|éclair|contre-attaques?|(?:un|en|le|du|vrai|ce) contre)\b",
               "es": r"(?i)\b(?:rápid[oa]s?|rápidamente|contragolpes?|contraataques?|a la contra)\b"}
MAX_SENTENCES, MAX_WORDS = 3, 60  # per language, all notes together


def meaning_issues(line: str, lang: str, claims: list[dict], facts: dict) -> list[dict]:
    """What the numbers mean, against the readings computed in code: a comparison that goes the wrong way (more space when the shooter had less), a team under the cosh told in control or the reverse, a player other than the shooter who finishes, a quick move that was not a quick counter, a space before % in English."""
    found = []
    claimed = {c["source_field"]: float(c["value"]) for c in claims}
    for s in re.split(r"(?<=[.!?])\s+", line):
        values = {v for v, _ in numbers_in(s)}
        more, less = bool(re.search(MORE_WORDS[lang], s)), bool(re.search(LESS_WORDS[lang], s))
        for first, second in PAIRS:
            if first not in facts or second not in facts or not any(f in claimed and claimed[f] in values for f in (first, second)) or not (more or less):
                continue
            a, b = float(facts[first]), float(facts[second])
            if (a > b and less and not more) or (a < b and more and not less) or a == b:
                found.append({"rule": "direction", "text": s})
        if "team_threat_share_before_shot_pct" in claimed and claimed["team_threat_share_before_shot_pct"] in values:
            reading = facts.get("threat_share_reading")
            if (reading == "under the cosh" and re.search(CONTROL_WORDS[lang], s)) or (reading == "in control" and re.search(RUN_OF_PLAY_WORDS[lang], s)):
                found.append({"rule": "threat_reading", "text": s})
        if re.search(FINISH_WORDS[lang], s):
            shooter = str(facts.get("shooter_name", ""))
            others = {str(facts[k]) for k in ("ball_winner_name", "key_passer_name", "key_pass_receiver_name") if facts.get(k) and facts[k] != shooter}
            if any(re.search(rf"\b{re.escape(n)}\b", s) or re.search(rf"\b{re.escape(n.split()[-1])}\b", s) and n.split()[-1] not in shooter.split() for n in others):
                found.append({"rule": "role", "text": s})
        if facts.get("move_tempo") != "quick counter" and re.search(TEMPO_WORDS[lang], s):
            found.append({"rule": "tempo", "text": s})
    if lang == "en" and re.search(r"\d[ \u00a0\u202f]%", line):
        found.append({"rule": "percent_space", "text": line})
    return found


def parse_analysis(text: Any) -> tuple[dict, bool]:
    """`{lang: [line, ...]}` of a reply (a JSON object, or the object found in the text), em dashes normalised as for every model text."""
    obj, valid = text, True
    if isinstance(text, str):
        try:
            obj = json.loads(text.strip())
        except json.JSONDecodeError:
            valid, found = False, re.search(r"\{.*\}", text, re.S)
            try:
                obj = json.loads(found.group(0)) if found else {}
            except json.JSONDecodeError:
                obj = {}
    if not isinstance(obj, dict):
        return {}, False
    return {str(lang): [{**l, "line": no_em_dash(l["line"])} if isinstance(l, dict) and isinstance(l.get("line"), str) else l for l in lines]
            for lang, lines in obj.items() if isinstance(lines, list)}, valid


def verify_analysis(texts: Any, languages: list[str], facts: dict) -> dict:
    """Checks analyst notes `{lang: [{"line", "claims"}]}`: 2 or 3 lines per requested language, and each line as a duo line (numbers claimed and matching their field, units, the style and on-air rules of `writer.style_issues`, names in the facts) plus `analysis_issues`."""
    texts = texts if isinstance(texts, dict) else {}
    report: dict[str, Any] = {"languages": {}, "lines": []}
    for lang in languages:
        lines = texts.get(lang) if isinstance(texts.get(lang), list) else []
        body = " ".join(normalise(str(l.get("line", ""))) for l in lines if isinstance(l, dict))
        n_sentences = len([x for x in re.split(r"(?<=[.!?])\s+", body.strip()) if x])
        report["languages"][lang] = {"count": len(lines), "count_ok": MIN_LINES <= len(lines) <= MAX_LINES, "sentences": n_sentences, "words": word_count(body),
                                     "length_ok": n_sentences <= MAX_SENTENCES and word_count(body) <= MAX_WORDS}
        for i, l in enumerate(lines):
            l = l if isinstance(l, dict) else {}
            line = normalise(str(l.get("line", "")))
            claims = l.get("claims") if isinstance(l.get("claims"), list) else []
            nums = check_numbers(line, claims, facts)
            errors, warnings = style_issues("", line, lang, nums["good"], facts)
            errors += analysis_issues(line, lang) + meaning_issues(line, lang, nums["good"], facts)
            warnings = [w for w in warnings if w["rule"] != "too_long"]
            if word_count(line) > LINE_MAX_WORDS:
                warnings.append({"rule": "too_long", "text": f"{word_count(line)} words"})
            entry = {"lang": lang, "index": i, "keys_ok": isinstance(l.get("line"), str) and bool(line.strip()), "bad_claims": nums["bad_claims"],
                     "unclaimed_numbers": nums["unclaimed_numbers"], "wrong_unit": nums["wrong_unit"], "unknown_names": names_ok(line, facts),
                     "style_errors": errors, "style_warnings": warnings}
            entry["verified"] = entry["keys_ok"] and not any(entry[k] for k in ("bad_claims", "unclaimed_numbers", "wrong_unit", "unknown_names", "style_errors"))
            report["lines"].append(entry)
    report["verified"] = all(v["count_ok"] and v["length_ok"] for v in report["languages"].values()) and all(e["verified"] for e in report["lines"])
    return report


def analysis_feedback(report: dict, json_valid: bool = True) -> list[str]:
    """What the Verifier rejected, one line per problem: the retry note and the trace."""
    lines = [] if json_valid else ["the reply was not one JSON object"]
    lines += [f"{lang}: 2 or 3 lines" for lang, v in report["languages"].items() if not v["count_ok"]]
    lines += [f"{lang}: too long, {v['sentences']} sentences and {v['words']} words (at most {MAX_SENTENCES} and {MAX_WORDS})" for lang, v in report["languages"].items()
              if not v.get("length_ok", True)]
    for e in report["lines"]:
        problems = [f"{k}: {e[k]}" for k in ("bad_claims", "unclaimed_numbers", "wrong_unit", "unknown_names") if e[k]] + [f"{i['rule']}: {i['text']}" for i in e["style_errors"]]
        if problems or not e["keys_ok"]:
            lines.append(f"{e['lang']} line {e['index'] + 1}: " + ("; ".join(problems) or "empty"))
    return lines


RULE_HINTS = {  # what each refusal means, in the retry note
    "too_many_numbers": "at most 2 numbers in a sentence, every digit group counted (metres, seconds, %): split the sentence or drop a number",
    "field_word": "never median, médiane, mediana, recent, \"change 3\" or a field name: say \"far more than usual\", \"on average\", \"in the last ten minutes\"",
    "data_dump": "never numbers in brackets: write a sentence",
    "article_before_team": "French: no article before a club name (never le Yarnford, du Varleigh, l'Yarnford, d'Yarnford): make the club the subject",
    "bad_claims": "each claim is a number field of get_analysis_facts with its exact value; there is no minute field: never write the minute",
    "unclaimed_numbers": "every number written needs a claim with its field; never write the minute",
    "wrong_unit": "% only for a field ending in _pct",
    "unknown_names": "no capitalised word that is not a name from the facts; sentence case",
    "offside_margin": "an offside is by a whisker or clear, never a distance",
    "league_points_verb": "points never with take, gain, win, prendre, gagner, ganar, sumar",
    "points_not_from_pts_field": "the word points only after a number from a field ending in _pts",
    "signed_number": "no + or - before a number: say up or down",
    "em_dash": "never the long dash",
    "direction": "a comparison must follow the numbers: use space_vs_typical, line_vs_average and threat_trend as given, never the other way round",
    "threat_reading": "follow threat_share_reading: a team under the cosh never had the advantage or control, a team in control was not against the run of play",
    "role": "only shooter_name shoots, finishes or scores; ball_winner_name won the ball, key_passer_name played the key pass",
    "tempo": "quick, rapid or counter only when move_tempo is quick counter; otherwise say direct move or patient build-up as move_tempo says",
    "percent_space": "English: no space before % (63%)",
    "too long": "at most 3 sentences and 60 words per language, all notes together",
    "forbidden_word": "never swing or momentum in French or Spanish",
    "probability_without_team": "French chance(s), Spanish probabilidad, opciones, posibilidades only in a sentence that names a club; say occasion, ocasión",
}


def retry_note(rejected: list[str]) -> str:
    """The Verifier's note for the retry: every problem it found, then what each refused rule means."""
    seen = [rule for rule in RULE_HINTS if any(rule in line for line in rejected)]
    hints = "; ".join(f"{rule}: {RULE_HINTS[rule]}" for rule in seen)
    return "rejected (" + " | ".join(rejected)[:1500] + ")." + (f" Fix: {hints}." if hints else "") + " Write all the notes again following the rules."


# --- the agent ------------------------------------------------------------------------------------

def _writer_rule(lang: str) -> str:
    """The Writer's language rule for `lang`, word for word (writer.WRITER_INSTRUCTIONS), so both agents get the same rule on clubs."""
    return next(line for line in WRITER_INSTRUCTIONS.splitlines() if line.startswith(f"- {lang}:"))


# A compliant reply for the 52' goal of the demo match, kept as data so that a test runs it through the Verifier against the real facts.
ANALYST_EXAMPLE = {
    "en": [{"line": "Varleigh were in control in the last ten minutes, with 63% of the threat after 28% before, so this had been coming.",
            "claims": [{"value": 63, "source_field": "team_threat_share_before_shot_pct"}, {"value": 28, "source_field": "team_threat_share_earlier_pct"}]},
           {"line": "A quick counter from deep: Brett Rivero's tackle 19 m from the Varleigh goal, and 12 s later the shot was away.",
            "claims": [{"value": 19, "source_field": "regain_from_own_goal_m"}, {"value": 12, "source_field": "move_duration_s"}]}],
    "fr": [{"line": "Varleigh avait la main sur les dix dernières minutes, avec 63 % de la menace après 28 % auparavant : ce but se préparait.",
            "claims": [{"value": 63, "source_field": "team_threat_share_before_shot_pct"}, {"value": 28, "source_field": "team_threat_share_earlier_pct"}]},
           {"line": "Un contre éclair parti de loin : le tacle de Brett Rivero à 19 m de son propre but, et 12 s plus tard le tir est parti.",
            "claims": [{"value": 19, "source_field": "regain_from_own_goal_m"}, {"value": 12, "source_field": "move_duration_s"}]}],
    "es": [{"line": "El Varleigh mandaba en los últimos diez minutos, con el 63% de la amenaza tras el 28% anterior: este gol se veía venir.",
            "claims": [{"value": 63, "source_field": "team_threat_share_before_shot_pct"}, {"value": 28, "source_field": "team_threat_share_earlier_pct"}]},
           {"line": "Un contragolpe rápido desde atrás: la entrada de Brett Rivero a 19 m de su propia portería, y 12 s después llega el tiro.",
            "claims": [{"value": 19, "source_field": "regain_from_own_goal_m"}, {"value": 12, "source_field": "move_duration_s"}]}],
}

ANALYST_INSTRUCTIONS = """You are the Analyst of a live football broadcast: for a goal or a big chance, you write 2 or 3 short tactical notes for the producer and the commentators, the way a TV pundit or a coach would explain the move. They are read in the control room, never voiced.

Facts: call the get_analysis_facts tool with the card_id. Use ONLY those facts: where and how the ball was won, the passes and the time it took, the pass that took the most defenders out, the shooter's space, the defending team's line, the share of the threat before the shot. Never invent a statistic, a name or a fact; never name a person who is not in the facts. Leave out what is not in the facts.

Readings: the code has already judged what the numbers mean. Use these facts as given, never judge a number yourself:
- move_tempo: quick counter, direct move or patient build-up.
- space_vs_typical: more room than usual, less room than usual or about usual.
- line_vs_average: higher than usual, deeper than usual or as usual.
- threat_share_reading: in control, even or under the cosh; threat_trend: rising, falling or steady.
Roles: shooter_name took the shot; ball_winner_name won the ball; key_passer_name played the key pass to key_pass_receiver_name. Never swap them.

What to write: what is not obvious from the replay. Pick the 2 or 3 strongest facts. Each note is one full sentence: say what happened and why it matters for the game (a high regain caught the defence unset, a quick counter, a pass that broke the lines, a shooter with more room than usual, a line pushed up and caught behind, a team in control or scoring against the run of play). At most 3 sentences and 60 words per language, all notes together.

Rules. The Verifier refuses a note that breaks any of them, and one refused note sends all of them back:
1. At most 2 numbers in a sentence. Every group of digits counts: metres, seconds, percentages, passes. "Went from 20 m to the Varleigh goal in 13 s using 5 passes" has 3 numbers and is refused; write "It started 20 m from the Varleigh goal, and 13 s later the shot was away."
2. Never write the minute ("at 52'", "à la 52'", "al 52'"): the console shows it. There is no minute field to claim.
3. Never the words median, médiane, mediana, recent, "match median", "change 3", and never a field name or a word with an underscore. Say "more room than usual in this match", "against 26 m on average", "in the last ten minutes" (the windows in words, never as a number).
4. Never numbers in brackets, never a list of numbers: full sentences only.
5. Every number in claims, with the field of get_analysis_facts it comes from and its exact value. Write each number as given, never compute a new one, no sign (never +30). Fields ending in _pct take a % sign, with no space before it in English (63%); _m are metres ("6.1 m", French and Spanish "6,1 m"); _s are seconds; _pts are percentage points: the word points (puntos) right after the number, never with take, gain, win, earn, prendre, gagner, ganar, sumar.
6. A comparison follows the readings: with less room than usual, never "more", "above", "bigger" (plus, au-dessus; más, por encima); with a line deeper than usual, never "higher"; a share that fell is never "up". A team under the cosh never "had the advantage", "took over" or "was in control" (la main, l'ascendant, domine; mandaba, domina, controla); a team in control never scored "against the run of play".
7. Only shooter_name shoots, finishes, strikes or scores (frappe, tire, marque, conclut; remata, dispara, marca). A sentence with such a verb names no other player.
8. Quick, rapid, fast or counter (rapide, éclair, contre-attaque, un contre; rápido, contragolpe, a la contra) only when move_tempo is quick counter.
9. Never a count of matches, a rate across the league, a ratio ("3 times more") or bits. An offside is "by a whisker" ("d'un rien", "por muy poco") or "clear" ("net", "claro"), never a distance; no centimetres.
10. Sentence case: a capital on the first word and on names from the facts only. Never the long dash (em dash). Plain English: "in their own half", never "in the own third".
11. French: never "chance" or "chances" in a sentence that does not name a club (say "occasion"). Spanish: never "probabilidad", "opciones" or "posibilidades" in a sentence that does not name a club (say "ocasión").
12. Clubs, as for the cards:
<writer_rules>

Bad and good:
- bad: "Yarnford's line played higher (39 m recent, 26 m match, change 13)." (numbers in brackets, field words, no reason)
- good: "Yarnford had pushed their back line up before the shot, their last defender 39 m from goal against 26 m on average, leaving space in behind."
- bad: "Méndez had 1.4 m of space, well above the 2.8 m typical reception." (less room than usual told as more, field word)
- good: "Méndez had 4.2 m of space when the ball arrived, more room than usual in this match, and nobody was close enough to block."
- bad: "Varleigh had 33% of the threat, carrying the advantage." (33% is under the cosh)
- bad: "Kosznik's pass released Carter to finish." when shooter_name is Méndez (only the shooter finishes)
- bad (French): "Le Yarnford presse, et la défense du Varleigh recule." good: "Yarnford presse, et Varleigh recule."

Output: reply with one JSON object only, no other text: {"en": [...], "fr": [...], "es": [...]} with the requested languages, each a list of 2 or 3 notes {"line": string, "claims": [{"value": number, "source_field": string}]}. The same notes in every language, each written natively, not translated word for word. A compliant reply for a goal (Varleigh, ball won by Brett Rivero's tackle 19 m from his own goal, move_tempo quick counter, the shot 12 s later; 63% of the threat in the ten minutes before the shot after 28% before, in control and rising):
<example>
"""
ANALYST_INSTRUCTIONS = ANALYST_INSTRUCTIONS.replace("<writer_rules>", "\n".join(_writer_rule(lang) for lang in ("fr", "es"))).replace(
    "<example>", json.dumps(ANALYST_EXAMPLE, ensure_ascii=False))
ANALYST_REPLY_RULE = "Reply with the JSON object only; every number in claims, from get_analysis_facts only."


class AnalystWriter(AgentWriter):
    """The Analyst agent on Microsoft Foundry (Agent Framework, Responses API), with get_analysis_facts as a function tool; the set-up of `writer.AgentWriter`. Its model, effort and output budget come from FOUNDRY_ANALYST_MODEL, _REASONING_EFFORT and _MAX_TOKENS, else the FOUNDRY_ ones."""

    instructions = ANALYST_INSTRUCTIONS
    agent_name = "analyst"
    env_prefix = "FOUNDRY_ANALYST"

    def __init__(self, model: str | None = None, **kwargs) -> None:
        import os

        super().__init__(model or os.environ.get("FOUNDRY_ANALYST_MODEL"), **kwargs)

    def tool(self):
        from agent_framework import tool

        store = self.facts

        @tool(approval_mode="never_require")
        def get_analysis_facts(card_id: str) -> str:
            """Return the tactical facts of the move that ended in the card's shot and the meaning of each field, as JSON."""
            if card_id not in store:
                return json.dumps({"error": f"unknown card_id {card_id}"})
            facts, meanings = store[card_id]
            return json.dumps({"facts": facts, "field_meanings": meanings}, ensure_ascii=False)

        return get_analysis_facts

    async def write_async(self, card_id: str, facts: tuple[dict, dict], languages: list[str], note: str | None = None, max_tokens: int | None = None) -> str:
        self.facts[card_id] = facts
        prompt = f"Write the analyst notes for this request: {json.dumps({'card_id': card_id, 'languages': languages})}"
        if note:
            prompt += f"\nVerifier feedback on your previous draft: {note}"
        return await self.run(f"{prompt}\n{ANALYST_REPLY_RULE}", max_tokens)


class DryAgent:
    """An offline agent for `--analyst-dry`: answers with the template notes of the facts the writer stored, so the whole model path runs without Azure."""

    def __init__(self, writer: "AnalystWriter | None" = None) -> None:
        self.writer = writer

    async def run(self, prompt: str, options: dict | None = None, **_) -> Any:
        card_id = json.loads(re.search(r"\{.*?\}", prompt).group(0))["card_id"]
        facts, _ = self.writer.facts[card_id]
        text = json.dumps(template_analysis(facts, list(LANGUAGES)), ensure_ascii=False)
        return type("Reply", (), {"text": text, "finish_reason": "stop", "usage_details": {}})()


def dry_analyst() -> AnalystWriter:
    writer = AnalystWriter(model="dry", agent=DryAgent())
    writer.agent.writer = writer
    return writer


async def analyse_async(card: dict, facts: dict, meanings: dict, writer=None, languages: list[str] = list(LANGUAGES), calls_left: int = 2) -> dict:
    """The analysis of a card: the model, one retry told what the Verifier rejected, then the template (`writer.model_attempts`). Returns the `analysis` document of the card."""
    texts, name, log, reason = None, "template", [], None
    model = writer is not None
    if model:
        async def call(note: str | None, max_tokens: int | None) -> str:
            return await writer.write_async(card["id"], (facts, meanings), languages, note, **({"max_tokens": max_tokens} if max_tokens else {}))

        def check(reply: str) -> tuple[bool, list[str], str]:
            nonlocal texts
            parsed, valid = parse_analysis(reply)
            report = verify_analysis(parsed, languages, facts)
            ok = valid and report["verified"]
            if ok:
                texts = {lang: parsed[lang] for lang in languages}
            rejected = analysis_feedback(report, valid)
            return ok, rejected, retry_note(rejected)

        log, reason = await model_attempts(writer, calls_left, call, check)
        if reason is None:
            name = writer.name
    config = getattr(writer, "config", None) if log else None
    return analysis_doc(card, facts, meanings, languages, texts, name, log, reason if model else None, config)


def analysis_doc(card: dict, facts: dict, meanings: dict, languages: list[str], texts: dict | None = None, writer: str = "template", log: list | None = None,
                 reason: str | None = None, config: dict | None = None) -> dict:
    """The `analysis` document of a card (docs/contracts.md): the texts given, else the template's, checked."""
    texts = texts if texts is not None else template_analysis(facts, languages)
    report = verify_analysis(texts, languages, facts)
    return {"status": "proposed", "card_version": card.get("version", 1), "facts_version": FACTS_VERSION, "texts": texts, "writer": writer,
            "verified": report["verified"], "machine_translated": sorted(MACHINE_TRANSLATED & set(languages)), "facts": facts, "field_meanings": meanings,
            "verification": {"attempts": len(log or []), "attempts_log": log or [], "fallback_reason": reason, "report": report},
            "writer_config": config if isinstance(config, dict) else None}


# --- on the desk ----------------------------------------------------------------------------------

class AnalystRunner:
    """Runs the analyses on a thread and an event loop of their own, one at a time in submission order; `results()` hands back the finished ones."""

    def __init__(self) -> None:
        self._results: queue.SimpleQueue = queue.SimpleQueue()
        self._futures: list[concurrent.futures.Future] = []
        self._loop = asyncio.new_event_loop()
        self._gate: asyncio.Semaphore | None = None
        self._thread = threading.Thread(target=self._loop.run_forever, name="analyst", daemon=True)
        self._thread.start()

    def submit(self, key: tuple, coro) -> None:
        self._futures = [f for f in self._futures if not f.done()]
        self._futures.append(asyncio.run_coroutine_threadsafe(self._run(key, coro), self._loop))

    async def _run(self, key: tuple, coro) -> None:
        if self._gate is None:
            self._gate = asyncio.Semaphore(1)
        async with self._gate:
            try:
                self._results.put((key, await coro, None))
            except Exception as error:  # the template takes over at apply
                self._results.put((key, None, error))

    def results(self) -> list[tuple]:
        out = []
        while True:
            try:
                out.append(self._results.get_nowait())
            except queue.Empty:
                return out

    def pending(self) -> int:
        return sum(1 for f in self._futures if not f.done())

    def close(self, timeout: float | None = None) -> None:
        concurrent.futures.wait(self._futures, timeout=timeout)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


class AnalystDesk:
    """A desk (`worker.desk.Desk` or `worker.agent_desk.AgentDesk`) with the Analyst after each goal, chance or save card: the card is written and stored by the desk as before, then its `analysis` is attached, stored and audited (`analysis_written`). `analyst` None writes the template at once; a model writer runs on `AnalystRunner`, applied at `poll`, within `max_calls_per_match` model calls of its own. `feed(match_id)` gives the feed's actions so far (`feed_actions`). Everything else is the desk's: the wrapper's own attributes start with `analyst` so that none hides one of the desk's."""

    def __init__(self, desk, feed, analyst=None, max_calls_per_match: int = 12) -> None:
        self.desk = desk
        self.analyst_feed = feed
        self.analyst = analyst
        self.analyst_max_calls = max_calls_per_match
        self.analyst_calls: dict[str, int] = {}  # match id -> model calls made (written on the Analyst's thread)
        self.analyst_lock = threading.Lock()
        self.analyst_live: dict[str, dict] = {}  # card id -> the card, while its analysis runs
        self.analyst_runner = AnalystRunner() if analyst is not None else None

    def __getattr__(self, name: str):
        return getattr(self.desk, name)

    def write(self, card: dict, teams: dict[str, str]) -> dict:
        out = self.desk.write(card, teams)
        self._analyse(card, teams)
        return out

    def on_action(self, card: dict, action: dict, teams: dict[str, str]) -> dict:
        """As the desk; a retraction marks the analysis retracted, a rewrite (the new version the desk wrote) gets a new analysis."""
        out = self.desk.on_action(card, action, teams)
        if action.get("action") == "retract":
            self._retract(card, str(action.get("cause", "")))
        elif action.get("action") == "rewrite":
            self._analyse(card, teams)
        return out

    def _analyse(self, card: dict, teams: dict[str, str]) -> None:
        try:
            got = analysis_facts(self.analyst_feed(card["match_id"]), card, teams) if card.get("moment") in ANALYSIS_MOMENTS else None
        except Exception:
            log.exception("analysis facts of %s not computed", card["id"])
            return
        if got is None:
            return
        facts, meanings = got
        match_id = card["match_id"]
        if self.analyst_runner is None:
            self._attach(card, analysis_doc(card, facts, meanings, self.desk.languages))
            return
        self.analyst_live[card["id"]] = card
        self.analyst_runner.submit((card["id"], card.get("version", 1), match_id), self._job(copy.deepcopy(card), facts, meanings, match_id))

    async def _job(self, card: dict, facts: dict, meanings: dict, match_id: str) -> dict:
        """One analysis on the Analyst's thread. The jobs run one at a time, so the calls left are those the jobs before really made: a call counts when it is made, never reserved, and the cap holds."""
        live = self.analyst_live.get(card["id"])
        if live is None or live.get("version", 1) != card.get("version", 1):
            return analysis_doc(card, facts, meanings, self.desk.languages, reason="superseded")  # rewritten meanwhile: dropped at poll, no call spent
        with self.analyst_lock:
            left = max(self.analyst_max_calls - self.analyst_calls.get(match_id, 0), 0)
        if left == 0:
            return analysis_doc(card, facts, meanings, self.desk.languages, reason="budget")
        try:
            doc = await analyse_async(card, facts, meanings, self.analyst, self.desk.languages, calls_left=min(2, left))
        except Exception:
            with self.analyst_lock:
                self.analyst_calls[match_id] = self.analyst_calls.get(match_id, 0) + min(2, left)  # unknown: counted as spent
            raise
        with self.analyst_lock:
            self.analyst_calls[match_id] = self.analyst_calls.get(match_id, 0) + doc["verification"]["attempts"]
        return doc

    def poll(self) -> int:
        """Applies the analyses finished since the last call, on the pipeline's thread, then the desk's own background work."""
        applied = 0
        for (card_id, version, match_id), doc, error in self.analyst_runner.results() if self.analyst_runner is not None else []:
            card = self.analyst_live.get(card_id)
            if card is None or card.get("version", 1) != version:
                continue  # a new version has its own analysis
            self.analyst_live.pop(card_id, None)
            try:
                if doc is None:
                    log.warning("analysis of %s failed: %s", card_id, error)
                    doc = analysis_doc(card, *analysis_facts(self.analyst_feed(match_id), card), self.desk.languages, reason=f"error:{type(error).__name__}")
                self._refresh(card)
                self._attach(card, doc)
                applied += 1
            except Exception:
                log.exception("analysis of %s not applied", card_id)
        inner = getattr(self.desk, "poll", None)
        return applied + ((inner() or 0) if inner is not None else 0)

    def close(self, timeout: float | None = None) -> None:
        if self.analyst_runner is not None:
            self.analyst_runner.close(timeout)
        inner = getattr(self.desk, "close", None)
        if inner is not None:
            inner(timeout)
        self.poll()

    def _refresh(self, card: dict) -> None:
        """The producer's decisions from the store before the card is written again, as the pipeline does."""
        from worker.pipeline import PRODUCER_FIELDS

        load = getattr(self.desk.sink, "load_card", None)
        stored = load(card["match_id"], card["id"]) if load else None
        if stored is not None and stored is not card:
            for key in PRODUCER_FIELDS:
                if key in stored:
                    card[key] = copy.deepcopy(stored[key])

    def _attach(self, card: dict, doc: dict) -> None:
        if card.get("status") == "retracted":
            doc |= {"status": "retracted", "retracted_cause": (card.get("ruled_out") or {}).get("cause")}
        card["analysis"] = doc
        v = doc["verification"]
        self.desk._audit(card, "analysis_written", doc["writer"], {"verified": doc["verified"], "attempts": v["attempts"], "attempts_log": v["attempts_log"],
                                                                   "fallback_reason": v["fallback_reason"], "writer_config": doc["writer_config"],
                                                                   **({"cause": "retracted"} if doc["status"] == "retracted" else {})})
        self.desk.sink.card(card)

    def _retract(self, card: dict, cause: str) -> None:
        doc = card.get("analysis")
        if not isinstance(doc, dict) or doc.get("status") == "retracted":
            return  # an analysis still running is marked when it is applied
        doc |= {"status": "retracted", "retracted_cause": cause or None}
        self.desk._audit(card, "analysis_retracted", "feed", {"cause": cause})
        self.desk.sink.card(card)
