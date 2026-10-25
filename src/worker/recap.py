"""Post-match analysis and audience previews: from the approved cards of a finished match to a recap, a fan text and an analyst text in several languages, every number checked.

Only `approved` cards are read: a retracted, rejected or pending card is never cited, and the points of the win-probability curve that belong to a retracted moment are dropped. Goals ruled out by VAR are told as such, with the reason and, for an offside, whether it was tight or clear (`disallowed_goals`, from the worker's `var_decision` or the desk's `ruled_out` on a retracted goal card, or passed explicitly), never as goals and never with the margin. "Points" in a text is only ever a gap between two chances: the card ranking value `value_pts` orders the biggest moments but is never written, and league table points are not facts. The code computes every number (`analyse`, then `recap_facts` with explicit field names, like `card_facts`); the model only writes, and lists in `claims` each number it used with the field it comes from. Each text goes through `writer.verify`, plus checks on length, player names, spelled-out numbers and retracted moments. A failed draft gets one retry, then a fixed template takes over.

The worker writes the recap at the final whistle (`build_recap` or `recap_async`); the command line does the same for a desk already written.

Usage:
    python -m worker.recap data/desk/demo-0007 [--writer template|foundry] [--fan-team home|away] [--out file]
    python -m worker.recap --cosmos demo-0007 [--writer ...]      # reads the cards and the match state from Cosmos DB, writes the recap back
"""

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from worker.duo import fr_de, fr_typography
from worker.writer import (LANGUAGES, MACHINE_TRANSLATED, air_issues, league_points_field, names_ok, no_em_dash, offside_call, sentences, shown, var_ruling, verify,
                           word_count)

MAX_WORDS = {"recap": 100, "fan": 50, "analyst": 60}
HALF_TIME_GAP_S = 120  # the match clock goes back to 45:00 at the start of the second half
TOP_MOMENTS = 3
SPELLED_NUMBERS = {  # numbers must be written in digits, or the Verifier cannot check them
    "en": "two three four five six seven eight nine ten eleven twelve twice",
    "fr": "deux trois quatre cinq six sept huit neuf dix onze douze",
    "es": "dos tres cuatro cinco seis siete ocho nueve diez once doce",
}


def slug(name: str) -> str:
    return re.sub(r"\W+", "_", name.lower()).strip("_")


def other(side: str) -> str:
    return "away" if side == "home" else "home"


RULED_OUT_WORDS = {"en": r"(?i)\bVAR\b|ruled out|disallowed", "fr": r"(?i)\bVAR\b|refusé|annulé", "es": r"(?i)\bVAR\b|anulad|anul[óo]"}  # a sentence that names a retracted minute must say it was ruled out
REASONS = {"offside": {"en": "offside", "fr": "hors-jeu", "es": "fuera de juego"}, "handball": {"en": "handball", "fr": "main", "es": "mano"}}


def ruled_out_by_var(card: dict) -> bool:
    """A retracted goal card VAR took away: the desk's `ruled_out` record, or the worker's `var_decision` for a VAR cause."""
    if card.get("status") != "retracted" or card.get("moment") != "goal":
        return False
    return bool(card.get("ruled_out")) or str((card.get("var_decision") or {}).get("reason") or "").startswith("var_")


def disallowed_goals(cards: list[dict]) -> list[dict]:
    """Goals VAR ruled out, in match order, with the reason, the offside call (tight or clear: `var_decision.call` first, so that it survives a store with no feed events) and the margin, which stays with the producer."""
    out = []
    for c in sorted((c for c in cards if ruled_out_by_var(c)), key=lambda c: c["clock_s"]):
        r = var_ruling(c)
        out.append({"card_id": c["id"], "minute_label": c["minute_label"], "team": c["team"], "player": c["player"]["name"], "reason": r["reason"],
                    "margin_cm": r["margin_cm"], "offside_call": r["call"]})
    return out


def load_desk(path: Path) -> tuple[list[dict], dict]:
    """Cards and match state of a local desk (`data/desk/<match_id>/`)."""
    path = Path(path)
    return json.loads((path / "cards.json").read_text(encoding="utf-8")), json.loads((path / "state.json").read_text(encoding="utf-8"))


# --- deterministic analysis -----------------------------------------------------------------------

def wp_around(card: dict) -> tuple[dict, dict]:
    """Each side's chance of winning just before and just after a goal or a red card, in % as on the card; the other side's is 100 minus the card side's and the draw."""
    f, side = card["facts"], card["team"]
    before = {side: f["team_wp_before_pct"], other(side): 100 - f["team_wp_before_pct"] - f["draw_before_pct"]}
    after = {side: f["team_wp_after_pct"], other(side): 100 - f["team_wp_after_pct"] - f["draw_after_pct"]}
    return {k: shown(max(v, 0)) for k, v in before.items()}, {k: shown(max(v, 0)) for k, v in after.items()}


def curve_point(label: str, p: dict, final: bool = False) -> dict:
    pct = (lambda x: round(100 * x)) if final else (lambda x: shown(round(100 * x)))
    return {"label": label, "minute_label": p["minute_label"], "home_pct": pct(p["home"]), "draw_pct": pct(p["draw"]), "away_pct": pct(p["away"])}


def analyse(cards: list[dict], state: dict, disallowed: list[dict] | None = None) -> dict:
    """Everything the texts may say, computed from the approved cards and the win-probability curve; `disallowed` (goals ruled out by VAR: minute_label, team, player, reason, margin_cm) defaults to what the retracted cards record."""
    teams, score = state["teams"], state["score"]
    raw = state.get("wp_timeline", [])
    order = {p["event_id"]: i for i, p in enumerate(raw)}
    position = lambda c: (order.get(c["event_id"], order.get(c.get("shot_event_id"), len(raw))), c["clock_s"])
    on_air = sorted((c for c in cards if c.get("status") == "approved"), key=position)
    retracted = [c for c in cards if c.get("status") == "retracted"]
    voided = {i for c in retracted for i in (c["event_id"], c.get("shot_event_id")) if i}
    curve = [p for p in raw if p["event_id"] not in voided]  # a retracted goal leaves a one-point spike on the live curve

    winner = "home" if score["home"] > score["away"] else "away" if score["away"] > score["home"] else None
    goals = [c for c in on_air if c["moment"] == "goal"]
    goals_consistent = all(sum(c["team"] == s for c in goals) == score[s] for s in ("home", "away"))
    half_time = next((i for i in range(1, len(raw)) if raw[i]["clock_s"] < raw[i - 1]["clock_s"] - HALF_TIME_GAP_S), None)

    out: dict[str, Any] = {"match_id": state["match_id"], "final": bool(state.get("final")), "teams": teams, "score": score, "winner": winner,
                           "goals_consistent": goals_consistent, "cards_on_air": [c["id"] for c in on_air], "cards_retracted": [c["id"] for c in retracted],
                           "disallowed_goals": disallowed_goals(cards) if disallowed is None else disallowed}
    if half_time is not None and goals_consistent:
        ht = {s: sum(c["team"] == s for c in goals if position(c)[0] < half_time) for s in ("home", "away")}
        out["half_time_score"] = ht

    # turning point: the approved goal or red card that raised the eventual winner's chance of winning the most; in a draw, the one that moved either side's chance the most
    candidates = []
    for c in (c for c in on_air if c["moment"] in ("goal", "red_card")):
        before, after = wp_around(c)
        side = winner or max(("home", "away"), key=lambda s: abs(after[s] - before[s]))
        candidates.append(((after[side] - before[side]) if winner else abs(after[side] - before[side]), c, side, before, after))
    best = max(candidates, key=lambda x: x[0], default=None)  # max keeps the earliest of equal swings
    if best and best[0] > 0:
        swing, c, side, before, after = best
        after_score = dict(c["score_before"])
        after_score[c["team"]] += c["moment"] == "goal"
        out["turning_point"] = {"card_id": c["id"], "minute_label": c["minute_label"], "moment": c["moment"], "team": c["team"], "player": c["player"]["name"],
                                "side": side, "wp_before_pct": before, "wp_after_pct": after, "swing_pts": swing, "score_after": after_score}

    top = sorted(on_air, key=lambda c: -c["value_pts"])[:TOP_MOMENTS]  # stable sort: equal values keep match order
    out["biggest_moments"] = [{"card_id": c["id"], "minute_label": c["minute_label"], "moment": c["moment"], "team": c["team"], "player": c["player"]["name"], "value_pts": c["value_pts"]}
                              for c in sorted(top, key=lambda c: -c["value_pts"])]

    summary = {}
    for s in ("home", "away"):
        scorers: dict[str, dict] = {}
        for c in (c for c in goals if c["team"] == s):
            entry = scorers.setdefault(c["player"]["id"], {"name": c["player"]["name"], "goals": 0, "minutes": []})
            entry["goals"] += 1
            entry["minutes"].append(c["minute_label"])
        summary[s] = {"name": teams[s], "goals": score[s], "red_cards": state.get("reds", {}).get(s, 0), "scorers": sorted(scorers.values(), key=lambda e: -e["goals"]),
                      "saves_on_air": sum(c["moment"] == "save" and c["team"] == s for c in on_air), "cards_on_air": sum(c["team"] == s for c in on_air)}
    out["team_summary"] = summary

    if curve:
        points = [curve_point("kick-off", curve[0])]
        if half_time is not None:
            ht_point = next((p for p in reversed(raw[:half_time]) if p["event_id"] not in voided), None)
            if ht_point:
                points.append(curve_point("half-time", ht_point))
        live = curve[:-1] or curve  # the last point comes after the final whistle when the match is over
        if winner:
            low = min(live, key=lambda p: p[winner])
            points.append(curve_point(f"{winner} lowest", low))
            last_below = max((i for i, p in enumerate(curve) if p[winner] <= 0.5), default=-1)
            if last_below + 1 < len(curve):
                points.append(curve_point(f"{winner} above 50% for good", curve[last_below + 1]))
        points.append(curve_point("final", curve[-1], final=out["final"]))
        out["wp_curve"] = points
    return out


def recap_facts(analysis: dict, fan_side: str | None = None) -> tuple[dict, dict, dict]:
    """Facts for the RecapWriter with explicit names, what each field means, and the roles the templates need."""
    teams, score, winner = analysis["teams"], analysis["score"], analysis["winner"]
    s = {side: slug(teams[side]) for side in ("home", "away")}
    fan = fan_side or winner or "home"
    analyst = winner or "home"
    line = lambda sc: f"{teams['home']} {sc['home']}-{sc['away']} {teams['away']}"
    facts: dict[str, Any] = {"home_team": teams["home"], "away_team": teams["away"], "final_score": line(score), "fan_team": teams[fan], "analyst_team": teams[analyst]}
    meanings = {"home_team": "home team", "away_team": "away team", "final_score": "final score", "fan_team": "team whose fans the fan text speaks to",
                "analyst_team": "team whose chance of winning the analyst text follows"}
    if winner:
        facts |= {"winner": teams[winner], "loser": teams[other(winner)]}
        meanings |= {"winner": "team that won the match", "loser": "team that lost the match"}
    else:
        facts["result"] = "draw"
        meanings["result"] = "the match ended level"
    if "half_time_score" in analysis:
        facts["half_time_score"] = line(analysis["half_time_score"])
        meanings["half_time_score"] = "score at half-time"
    for side, summary in analysis["team_summary"].items():
        name, k = teams[side], s[side]
        facts[f"{k}_goals"] = summary["goals"]
        facts[f"{k}_red_cards"] = summary["red_cards"]
        meanings |= {f"{k}_goals": f"goals scored by {name}", f"{k}_red_cards": f"red cards shown to {name} players"}
        if summary["scorers"] and analysis["goals_consistent"]:
            top = summary["scorers"][0]
            facts |= {f"{k}_top_scorer": top["name"], f"{k}_top_scorer_goals": top["goals"]}
            meanings |= {f"{k}_top_scorer": f"{name} player who scored the most goals", f"{k}_top_scorer_goals": f"goals scored by {k}_top_scorer"}

    for point in analysis.get("wp_curve", []):
        label = point["label"]
        if label in ("kick-off", "half-time"):
            tag = "kickoff" if label == "kick-off" else "half_time"
            for side in ("home", "away"):
                facts[f"{s[side]}_wp_{tag}_pct"] = point[f"{side}_pct"]
                meanings[f"{s[side]}_wp_{tag}_pct"] = f"{teams[side]}'s chance of winning the match at {label}, in %"
        elif label.endswith("lowest"):
            k = s[winner]
            facts |= {f"{k}_lowest_wp_pct": point[f"{winner}_pct"], f"{k}_lowest_wp_minute": point["minute_label"]}
            meanings |= {f"{k}_lowest_wp_pct": f"lowest chance of winning {teams[winner]} had during the match, in %", f"{k}_lowest_wp_minute": f"minute of {k}_lowest_wp_pct"}
        elif label.endswith("for good"):
            k = s[winner]
            facts |= {f"{k}_above_50_for_good_from": point["minute_label"], "majority_line_pct": 50}
            meanings |= {f"{k}_above_50_for_good_from": f"minute from which {teams[winner]}'s chance of winning stayed above 50% until the end",
                         "majority_line_pct": "the 50% line of a chance of winning, in %"}

    tp = analysis.get("turning_point")
    if tp:
        name = teams[tp["team"]]
        facts |= {"turning_point_minute": tp["minute_label"], "turning_point_event": f"{name} {'goal' if tp['moment'] == 'goal' else 'player sent off'}",
                  "turning_point_player": tp["player"], "turning_point_team": teams[tp["side"]], "turning_point_swing_pts": tp["swing_pts"]}
        meanings |= {"turning_point_minute": "minute of the turning point", "turning_point_event": "what happened at the turning point",
                     "turning_point_player": "player who scored, or was sent off, at the turning point", "turning_point_team": "team whose chance of winning the turning point moved (the winner, or in a draw the side it moved most)",
                     "turning_point_swing_pts": "size of the change in turning_point_team's chance of winning at the turning point, in percentage points"}
        if tp["moment"] == "goal":
            facts["turning_point_score_after"] = line(tp["score_after"])
            meanings["turning_point_score_after"] = "score just after the turning point"
        for side in ("home", "away"):
            facts[f"turning_point_{s[side]}_wp_before_pct"] = tp["wp_before_pct"][side]
            facts[f"turning_point_{s[side]}_wp_after_pct"] = tp["wp_after_pct"][side]
            meanings[f"turning_point_{s[side]}_wp_before_pct"] = f"{teams[side]}'s chance of winning just before the turning point, in %"
            meanings[f"turning_point_{s[side]}_wp_after_pct"] = f"{teams[side]}'s chance of winning just after the turning point, in %"
    events = {"goal": "goal", "save": "goalkeeper save", "red_card": "player sent off", "chance": "chance missed"}
    for i, m in enumerate(analysis["biggest_moments"], start=1):  # ranked by value_pts, which is never written: "points" on screen is a change in a chance of winning
        facts |= {f"moment_{i}_minute": m["minute_label"], f"moment_{i}_event": f"{teams[m['team']]} {events[m['moment']]}", f"moment_{i}_player": m["player"]}
        meanings |= {f"moment_{i}_minute": f"minute of the number {i} moment by how far it moved the result", f"moment_{i}_event": f"what happened at moment {i}", f"moment_{i}_player": f"player credited with moment {i}"}
    for i, g in enumerate(analysis.get("disallowed_goals", []), start=1):
        facts |= {f"disallowed_{i}_minute": g["minute_label"], f"disallowed_{i}_team": teams[g["team"]], f"disallowed_{i}_player": g["player"], f"disallowed_{i}_reason": g["reason"]}
        meanings |= {f"disallowed_{i}_minute": f"minute of goal {i} that VAR ruled out: it does not count", f"disallowed_{i}_team": f"team whose goal {i} was ruled out",
                     f"disallowed_{i}_player": f"player whose goal {i} was ruled out", f"disallowed_{i}_reason": f"why VAR ruled goal {i} out (offside, handball or other)"}
        call = g.get("offside_call") if g.get("offside_call") in ("tight", "clear") else offside_call(g["margin_cm"]) if isinstance(g.get("margin_cm"), int) else None
        if call and g["reason"] == "offside":
            facts[f"disallowed_{i}_offside_call"] = call
            meanings[f"disallowed_{i}_offside_call"] = f"how close the offside was for goal {i}: tight (say by a whisker) or clear; the margin never goes on air"

    roles = {"F": s[fan], "R": s[analyst], "W": s[winner] if winner else None, "T": s[tp["side"]] if tp else None,
             "fan_result": "draw" if not winner else "won" if fan == winner else "lost", "tp_moment": tp["moment"] if tp else None,
             "moments": len(analysis["biggest_moments"]), "disallowed": len(analysis.get("disallowed_goals", []))}
    return facts, meanings, roles


# --- Verifier -------------------------------------------------------------------------------------

def parse_drafts(text: str) -> tuple[list[dict], bool]:
    try:
        drafts, valid = json.loads(text.strip()), True
    except json.JSONDecodeError:
        found, valid = re.search(r"\[.*\]", text, re.S), False
        try:
            drafts = json.loads(found.group(0)) if found else []
        except json.JSONDecodeError:
            drafts = []
    if not isinstance(drafts, list) or not all(isinstance(d, dict) for d in drafts):
        drafts = []
    drafts = [{**d, **{k: no_em_dash(d[k]) for k in MAX_WORDS if isinstance(d.get(k), str)}} for d in drafts]  # no em dash on air
    return drafts, valid


def verify_recap(text: str, languages: list[str], facts: dict, cards: list[dict]) -> tuple[list[dict], dict]:
    """`writer.verify` on each text (numbers claimed, claims matching their field, percentages from % fields), then length, names, spelled-out numbers, retracted moments and the on-air rules (`writer.air_issues`: no league points verb, no league-wide rate, no em dash)."""
    drafts, valid = parse_drafts(text)
    allowed = " ".join(str(v) for v in facts.values())
    players = {c["player"]["name"] for c in cards}
    retracted_minutes = {c["minute_label"] for c in cards if c.get("status") == "retracted"} - {v for k, v in facts.items() if isinstance(v, str) and not k.startswith("disallowed_")}
    report: dict[str, Any] = {"json_valid": valid, "languages_ok": sorted(str(d.get("lang")) for d in drafts) == sorted(languages), "texts": []}
    for d in drafts:
        claims = d.get("claims") if isinstance(d.get("claims"), list) else []
        for section, limit in MAX_WORDS.items():
            body = d.get(section)
            if not isinstance(body, str):
                report["texts"].append({"lang": d.get("lang"), "section": section, "verified": False, "missing": True})
                continue
            _, base = verify(json.dumps([{"headline": body, "why": "","lang": d.get("lang"), "claims": claims}], ensure_ascii=False), [d.get("lang")], facts, style=False)
            entry = {k: v for k, v in base["cards"][0].items() if k not in ("length_ok", "lang")}  # writer.verify measures card lengths
            spelled = SPELLED_NUMBERS.get(str(d.get("lang")), "").split()
            entry |= {
                "lang": d.get("lang"), "section": section, "words": word_count(body), "length_ok": word_count(body) <= limit,
                "unknown_players": sorted(p for p in players if any(n in body and n not in allowed for n in (p, p.split()[-1]))),
                "spelled_numbers": [w for w in re.findall(r"[\wÀ-ÿ]+", body.lower()) if w in spelled],
                "retracted_minutes": sorted({m for m in retracted_minutes for s in sentences(body) if m in s and not re.search(RULED_OUT_WORDS.get(str(d.get("lang")), "VAR"), s)}),
                "air_errors": air_issues(body, str(d.get("lang"))),
            }
            entry["verified"] = (entry["verified"] and entry["length_ok"] and not entry["unknown_names"] and not entry["unknown_players"]
                                 and not entry["spelled_numbers"] and not entry["retracted_minutes"] and not entry["air_errors"])
            report["texts"].append(entry)
    report["verified"] = bool(drafts) and valid and report["languages_ok"] and all(e["verified"] for e in report["texts"])
    return drafts, report


# --- writers --------------------------------------------------------------------------------------

TP_PHRASE = {
    "goal": {"en": "a goal by {turning_point_player}", "fr": "le but de {turning_point_player}", "es": "el gol de {turning_point_player}"},
    "red_card": {"en": "the red card for {turning_point_player}", "fr": "l'expulsion de {turning_point_player}", "es": "la expulsión de {turning_point_player}"},
}

TEMPLATES = {  # each sentence is used only when every field it names is in the facts; French without article before a club, Spanish with it
    "recap": {
        "en": ["Full time: {final_score}.", "At half-time it was {half_time_score}.", "{W} fell as low as {W_lowest_wp_pct}% to win at {W_lowest_wp_minute}.",
               "The turning point came at {turning_point_minute}: {tp} took {T} from {turning_point_T_wp_before_pct}% to {turning_point_T_wp_after_pct}% to win, a swing of {turning_point_swing_pts} points.",
               "{W_top_scorer} scored {W_top_scorer_goals} goals."],
        "fr": ["Score final : {final_score}.", "À la mi-temps, {half_time_score}.", "{W} est tombé à {W_lowest_wp_pct} % de chances de gagner ({W_lowest_wp_minute}).",
               "Le tournant, à la {turning_point_minute} : {tp} fait passer {T} de {turning_point_T_wp_before_pct} % à {turning_point_T_wp_after_pct} %, {turning_point_swing_pts} points de bascule.",
               "{W_top_scorer} a marqué {W_top_scorer_goals} buts."],
        "es": ["Final: {final_score}.", "Al descanso, {half_time_score}.", "El {W} cayó al {W_lowest_wp_pct}% de opciones de ganar ({W_lowest_wp_minute}).",
               "El punto de inflexión, en el {turning_point_minute}: {tp} llevó al {T} del {turning_point_T_wp_before_pct}% al {turning_point_T_wp_after_pct}%, {turning_point_swing_pts} puntos de cambio.",
               "{W_top_scorer} marcó {W_top_scorer_goals} goles."],
    },
    "fan": {  # a draw is never "a point": points on air are only a gap between two chances
        "en": {"won": "What a result for {F}: {final_score}.", "lost": "Hard to take for {F}: {final_score}.", "draw": "Honours even for {F}: {final_score}.",
               "low": "Down to {F_lowest_wp_pct}% to win at {F_lowest_wp_minute}, they never gave up.", "tp": "{TP} at {turning_point_minute} took {F} from {turning_point_F_wp_before_pct}% to {turning_point_F_wp_after_pct}% to win.",
               "hero": "{F_top_scorer}, {F_top_scorer_goals} goals: a night to remember."},
        "fr": {"won": "Quelle soirée pour {F} : {final_score}.", "lost": "Dur à avaler pour {F} : {final_score}.", "draw": "Match nul pour {F} : {final_score}.",
               "low": "Tombé à {F_lowest_wp_pct} % de chances de gagner ({F_lowest_wp_minute}), {F} n'a jamais lâché.", "tp": "{TP}, à la {turning_point_minute}, a fait passer {F} de {turning_point_F_wp_before_pct} % à {turning_point_F_wp_after_pct} % de chances de gagner.",
               "hero": "{F_top_scorer} : {F_top_scorer_goals} buts, une soirée inoubliable."},
        "es": {"won": "Nochón para el {F}: {final_score}.", "lost": "Duro golpe para el {F}: {final_score}.", "draw": "Empate para el {F}: {final_score}.",
               "low": "Con solo un {F_lowest_wp_pct}% de opciones de ganar en el {F_lowest_wp_minute}, el {F} nunca se rindió.", "tp": "{TP} en el {turning_point_minute} llevó al {F} del {turning_point_F_wp_before_pct}% al {turning_point_F_wp_after_pct}% de opciones de ganar.",
               "hero": "{F_top_scorer}: {F_top_scorer_goals} goles, una noche para recordar."},
    },
    "analyst": {
        "en": {"curve": "{R}: {R_wp_kickoff_pct}% to win at kick-off", "ht": ", {R_wp_half_time_pct}% at half-time", "low": ", low of {R_lowest_wp_pct}% at {R_lowest_wp_minute}",
               "good": "Above {majority_line_pct}% for good from {R_above_50_for_good_from}.", "tp": "Biggest swing: {turning_point_minute}, {turning_point_swing_pts} points ({turning_point_T_wp_before_pct}% to {turning_point_T_wp_after_pct}%).",
               "moments": "Top moments: ", "moment": "{moment_N_minute}"},
        "fr": {"curve": "{R} : {R_wp_kickoff_pct} % de chances de gagner au coup d'envoi", "ht": ", {R_wp_half_time_pct} % à la mi-temps", "low": ", plancher à {R_lowest_wp_pct} % ({R_lowest_wp_minute})",
               "good": "Au-dessus de {majority_line_pct} % pour de bon à partir de la {R_above_50_for_good_from}.", "tp": "Plus forte bascule : {turning_point_minute}, {turning_point_swing_pts} points (de {turning_point_T_wp_before_pct} % à {turning_point_T_wp_after_pct} %).",
               "moments": "Moments les plus lourds : ", "moment": "{moment_N_minute}"},
        "es": {"curve": "{R}: {R_wp_kickoff_pct}% de opciones de ganar al inicio", "ht": ", {R_wp_half_time_pct}% al descanso", "low": ", mínimo del {R_lowest_wp_pct}% ({R_lowest_wp_minute})",
               "good": "Por encima del {majority_line_pct}% para siempre desde el {R_above_50_for_good_from}.", "tp": "Mayor cambio: {turning_point_minute}, {turning_point_swing_pts} puntos (del {turning_point_T_wp_before_pct}% al {turning_point_T_wp_after_pct}%).",
               "moments": "Momentos de más peso: ", "moment": "{moment_N_minute}"},
    },
}


DISALLOWED = {  # goals ruled out by VAR: one sentence in the recap, one in the analyst text; an offside is tight or clear, never a margin
    "recap": {"en": ("VAR ruled out {items}.", "{player}'s goal at {minute}", " and "), "fr": ("La VAR a refusé {items}.", "le but {de_player} à la {minute}", " et "),
              "es": ("El VAR anuló {items}.", "el gol de {player} en el {minute}", " y ")},
    "analyst": {"en": ("VAR: {items}.", "{minute}{reason}", ", "), "fr": ("VAR : {items}.", "{minute}{reason}", ", "), "es": ("VAR: {items}.", "{minute}{reason}", ", ")},
    "offside_call": {"en": {"tight": " (offside by a whisker)", "clear": " (clear offside)"}, "fr": {"tight": " (hors-jeu d'un rien)", "clear": " (hors-jeu net)"},
                     "es": {"tight": " (fuera de juego por muy poco)", "clear": " (fuera de juego claro)"}},
    "reason_only": {"en": " ({reason})", "fr": " ({reason})", "es": " ({reason})"},
}
TEMPLATES = {section: {lang: fr_typography(v) if lang == "fr" else v for lang, v in by_lang.items()} for section, by_lang in TEMPLATES.items()}
DISALLOWED = {section: {lang: fr_typography(v) if lang == "fr" else v for lang, v in by_lang.items()} for section, by_lang in DISALLOWED.items()}


def disallowed_sentence(section: str, lang: str, facts: dict, count: int) -> str:
    """The sentence about goals VAR ruled out, with placeholders on the disallowed_* facts (filled and claimed by RecapTemplateWriter.fill)."""
    head, item, sep = DISALLOWED[section][lang]
    items = []
    for i in range(1, count + 1):
        player = "{disallowed_%d_player}" % i
        de_player = ("d'" if fr_de(str(facts.get(f"disallowed_{i}_player", "x"))).startswith("d'") else "de ") + player
        reason = REASONS.get(facts.get(f"disallowed_{i}_reason"), {}).get(lang)
        text = item.replace("{de_player}", de_player).replace("{player}", player).replace("{minute}", "{disallowed_%d_minute}" % i)
        detail = ""
        if f"disallowed_{i}_offside_call" in facts:
            detail = DISALLOWED["offside_call"][lang][facts[f"disallowed_{i}_offside_call"]]
        elif reason:
            detail = DISALLOWED["reason_only"][lang].replace("{reason}", reason)
        items.append(text.replace("{reason}", detail) if "{reason}" in text else text + detail)
    joined = items[0] if len(items) == 1 else (", ".join(items[:-1]) + sep + items[-1] if section == "recap" else sep.join(items))
    return head.replace("{items}", joined)


TEAM_FIELDS = {"F": "fan_team", "R": "analyst_team", "W": "winner", "T": "turning_point_team"}


class RecapTemplateWriter:
    """Fixed sentences filled from the facts: no model, always verified."""

    name = "template"

    def __init__(self, roles: dict) -> None:
        self.roles = roles

    def _key(self, key: str) -> str | None:
        if key in TEAM_FIELDS:  # {F} alone is the fan team's name
            return TEAM_FIELDS[key]
        parts = [self.roles.get(p) if p in ("F", "R", "W", "T") else p for p in key.split("_")]
        return None if None in parts else "_".join(parts)

    def fill(self, text: str, facts: dict) -> tuple[str, list[dict]] | None:
        """The sentence with its fields filled and their numbers claimed, or None when a field is missing."""
        claims: list[dict] = []
        keys = [self._key(k) for k in re.findall(r"\{(\w+)\}", text)]
        if any(k is None or k not in facts for k in keys):
            return None
        for k in keys:
            claims += [{"value": int(n), "source_field": k} for n in re.findall(r"\d+", str(facts[k]))]
        return re.sub(r"\{(\w+)\}", lambda m: str(facts[self._key(m.group(1))]), text), claims

    def _join(self, parts: list[str], facts: dict, limit: int) -> tuple[str, list[dict]]:
        """The sentences whose fields are all in the facts; the last ones are dropped while the text is over `limit` words."""
        filled = [f for f in (self.fill(p, facts) for p in parts) if f]
        while len(filled) > 1 and word_count(" ".join(t for t, _ in filled)) > limit:
            filled.pop()
        return " ".join(t for t, _ in filled), [c for _, cl in filled for c in cl]

    def season_sentence(self, lang: str, facts: dict) -> str:
        """What the result changed in the table: the rank once the whole matchday is played when the storyline gives it, else the rank with this result only, told as provisional."""
        s = self.roles["season_slug"]
        final = f"season_{s}_rank_after_matchday" in facts
        f = {"rb": f"season_{s}_rank_before", "ra": f"season_{s}_rank_after_matchday" if final else f"season_{s}_rank_after",
             "cb": f"season_{s}_relegation_chance_before_pct", "ca": f"season_{s}_relegation_chance_after_pct"}
        same = facts[f["rb"]] == facts[f["ra"]]
        text = SEASON["matchday" if final else "provisional"]["stay" if same else "move"][lang].replace("{club}", self.roles["season_club"])
        for k in ("rb", "ra"):
            n = int(facts[f[k]])
            text = text.replace("{o_%s}" % k, ordinal_suffix(lang, n))
        for k, field in f.items():
            text = text.replace("{%s}" % k, "{%s}" % field)
        return fr_typography(text) if lang == "fr" else text

    def write(self, facts: dict, languages: list[str]) -> str:
        r, out = self.roles, []
        for lang in languages:
            phrase = TP_PHRASE.get(r["tp_moment"] or "", {}).get(lang, "")
            if lang == "fr" and str(facts.get("turning_point_player", " "))[0] in "AEIOUYÉÈH":
                phrase = phrase.replace("de {", "d'{")  # le but d'Ivo Ellery
            final, ht, low, tp, scorer = (s.replace("{tp}", phrase) for s in TEMPLATES["recap"][lang])
            recap = [final, ht]
            if r["W"] and facts.get(f"{r['W']}_lowest_wp_pct", 100) < 50:  # only when the winner was ever behind in the odds
                recap.append(low)
            recap.append(tp)
            if r.get("disallowed"):
                recap.append(disallowed_sentence("recap", lang, facts, r["disallowed"]))
            if r["W"] and facts.get(f"{r['W']}_top_scorer_goals", 0) >= 2:
                recap.append(scorer)
            if r.get("season_slug"):
                recap.append(self.season_sentence(lang, facts))
            fan_t = TEMPLATES["fan"][lang]
            fan = [fan_t[r["fan_result"]]]
            if r["fan_result"] == "won" and facts.get(f"{r['F']}_lowest_wp_pct", 100) <= 25:
                fan.append(fan_t["low"])
            if phrase:
                fan.append(fan_t["tp"].replace("{TP}", phrase[0].upper() + phrase[1:]))
            if r["fan_result"] == "won" and facts.get(f"{r['F']}_top_scorer_goals", 0) >= 2:
                fan.append(fan_t["hero"])
            a = TEMPLATES["analyst"][lang]
            first = self.fill(a["curve"], facts)
            analyst_parts = []
            if first:
                curve = a["curve"] + "".join(p for p in (a["ht"], a["low"]) if self.fill(p, facts)) + "."
                analyst_parts.append(curve)
            analyst_parts += [a["good"], a["tp"]]
            if r.get("disallowed"):
                analyst_parts.append(disallowed_sentence("analyst", lang, facts, r["disallowed"]))
            moments = [a["moment"].replace("_N_", f"_{i}_") for i in range(1, r["moments"] + 1)]
            if moments:
                analyst_parts.append(a["moments"] + ", ".join(moments) + ".")
            texts = {k: self._join(parts, facts, MAX_WORDS[k]) for k, parts in (("recap", recap), ("fan", fan), ("analyst", analyst_parts))}
            claims = [c for _, cl in texts.values() for c in cl]
            unique = [dict(t) for t in {tuple(sorted(c.items())) for c in claims}]
            out.append({"lang": lang, **{k: v[0] for k, v in texts.items()}, "claims": sorted(unique, key=lambda c: (c["source_field"], c["value"]))})
        return json.dumps(out, ensure_ascii=False)


RECAP_INSTRUCTIONS = """You write the post-match texts of a football broadcast.

Facts: call the get_recap_facts tool with the match_id to get the facts and the meaning of each field. Use ONLY those facts. Never invent a statistic, name, minute or fact, and never mention a moment that is not in the facts.

Goals ruled out by VAR are in the facts as disallowed_* fields: mention them in the recap, only as ruled out ("VAR ruled out Lightfoot's goal at 63': offside by a whisker"), in the same sentence as the words VAR or ruled out, with the reason. For an offside, disallowed_*_offside_call says whether it was tight ("offside by a whisker") or clear ("clear offside"): never give a margin. They never count as goals.

Each number has a field name that says what it means: never use a number with another meaning. Probabilities are already percentages: write them with a % sign (34%, never 0.34). Values ending in _pts are gaps between two chances, in percentage points: write them as points. The word points means nothing else, and never goes with a verb of league points (take, gain; French prendre, gagner; Spanish ganar, sumar). Do not compute any new number. Write every number in digits (4 goals, never "four goals"), and use no proper noun that is not in the facts.

The table: season_<club>_rank_after_matchday is the rank once the whole matchday is played. When only season_<club>_rank_after is given, it counts this result alone: say the rank is provisional ("provisionally 14th", "provisoirement 14e", "provisionalmente 14.º").

On air: a rarity is told in seasons, for the team or the player, never as a count of matches, a league-wide rate or a ratio. Never use the long dash (em dash): a comma, a colon or a full stop instead.

For each requested language write three texts:
- recap: the match in at most <recap_words> words, neutral, built around the turning point.
- fan: at most <fan_words> words, emotional, from the point of view of fan_team's supporters.
- analyst: at most <analyst_words> words, numbers-led, following analyst_team's chance of winning (kick-off, half-time, low point, turning point, biggest moments).
Write each language natively, with the same facts in every language: French without an article before a club name ("pour Rovers"), Spanish with it ("para el Rovers").

Reply with valid JSON only, no other text: a JSON array with one object per requested language:
{"lang": string, "recap": string, "fan": string, "analyst": string, "claims": [{"value": number, "source_field": string}]}
claims lists every number written in any of the three texts, with the field it comes from (a number taken from a score or a minute claims that field)."""
for _section, _limit in MAX_WORDS.items():  # the limits the Verifier applies
    RECAP_INSTRUCTIONS = RECAP_INSTRUCTIONS.replace(f"<{_section}_words>", str(_limit))


class RecapWriter:
    """The recap agent on Microsoft Foundry (Agent Framework, Responses API), with get_recap_facts as a function tool. Same set-up as `writer.FoundryWriter`."""

    def __init__(self, model: str | None = None, effort: str | None = None, max_tokens: int = 3000) -> None:
        from agent_framework import Agent, tool
        from agent_framework.foundry import FoundryChatClient
        from azure.identity import AzureCliCredential, ChainedTokenCredential, ManagedIdentityCredential

        self.model = model or os.environ["FOUNDRY_MODEL"]
        self.name = self.model
        self.facts: dict[str, tuple[dict, dict]] = {}
        store = self.facts

        @tool(approval_mode="never_require")
        def get_recap_facts(match_id: str) -> str:
            """Return the post-match facts of a match and the meaning of each field, as JSON."""
            if match_id not in store:
                return json.dumps({"error": f"unknown match_id {match_id}"})
            facts, meanings = store[match_id]
            return json.dumps({"facts": facts, "field_meanings": meanings}, ensure_ascii=False)

        credential = ChainedTokenCredential(ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID")), AzureCliCredential())
        client = FoundryChatClient(project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], model=self.model, credential=credential)
        options: dict[str, Any] = {"max_tokens": max_tokens}
        effort = effort or os.environ.get("FOUNDRY_REASONING_EFFORT")
        if effort:
            options["reasoning"] = {"effort": effort}
        self.agent = Agent(client=client, name="recap-writer", instructions=RECAP_INSTRUCTIONS, tools=[get_recap_facts], default_options=options)

    async def write_async(self, match_id: str, facts: tuple[dict, dict], languages: list[str], note: str | None = None) -> str:
        self.facts[match_id] = facts
        prompt = f"Write the post-match texts for this request: {json.dumps({'match_id': match_id, 'languages': languages})}"
        if note:
            prompt += f"\nNote from the previous attempt: {note}"
        response = await self.agent.run(prompt)
        return response.text


def failures(report: dict) -> str:
    """A short list of what the Verifier rejected, sent back to the model on the retry."""
    bad = [e for e in report.get("texts", []) if not e.get("verified")]
    keys = ("bad_claims", "unclaimed_numbers", "wrong_unit", "unknown_names", "unknown_players", "spelled_numbers", "retracted_minutes")
    return "; ".join(f"{e['lang']} {e['section']}: " + ", ".join([k for k in keys if e.get(k)] + [f"{i['rule']} ({i['text']})" for i in e.get("air_errors", [])])
                     + ("" if e.get("length_ok", True) else " too long") for e in bad)[:600] or "invalid JSON or missing languages"


async def recap_async(cards: list[dict], state: dict, writer=None, languages: list[str] = list(LANGUAGES), fan_side: str | None = None, calls_left: int = 2,
                      disallowed: list[dict] | None = None, storyline: dict | None = None) -> dict:
    """Analyses the match, writes and verifies the texts: the model, one retry, then the template. `disallowed` overrides the goals ruled out read from the cards; `storyline` (the pre-match document) adds what the result changed in the table."""
    analysis = analyse(cards, state, disallowed)
    facts, meanings, roles = recap_facts(analysis, fan_side)
    if storyline is not None and analysis["final"]:
        season_facts(storyline, analysis, facts, meanings, roles)
    attempts, note, texts, report, name = 0, None, None, None, "template"
    if writer is not None and not isinstance(writer, RecapTemplateWriter):
        for _ in range(min(2, calls_left)):
            attempts += 1
            try:
                text = await writer.write_async(state["match_id"], (facts, meanings), languages, note)
            except Exception as error:  # a model or network failure falls through to the template
                text = json.dumps({"error": f"{type(error).__name__}: {error}"[:300]})
            texts, report = verify_recap(text, languages, facts, cards)
            if report["verified"]:
                name = writer.name
                break
            note = failures(report)
    if not report or not report["verified"]:
        texts, report = verify_recap(RecapTemplateWriter(roles).write(facts, languages), languages, facts, cards)
    cited = [analysis["turning_point"]["card_id"]] if "turning_point" in analysis else []
    cited += [m["card_id"] for m in analysis["biggest_moments"] if m["card_id"] not in cited]
    return {"id": f"recap-{state['match_id']}", "type": "recap", "match_id": state["match_id"], "final": analysis["final"], "fan_team": facts["fan_team"],
            "analyst_team": facts["analyst_team"], "texts": texts, "machine_translated": sorted(MACHINE_TRANSLATED & set(languages)), "writer": name,
            "verification": {"verified": report["verified"], "attempts": attempts, "report": report}, "cited_cards": cited,
            "analysis": analysis, "facts": facts, "field_meanings": meanings}


def build_recap(cards: list[dict], state: dict, writer=None, languages: list[str] = list(LANGUAGES), fan_side: str | None = None, disallowed: list[dict] | None = None,
                storyline: dict | None = None) -> dict:
    return asyncio.run(recap_async(cards, state, writer, languages, fan_side, disallowed=disallowed, storyline=storyline))


SEASON = {  # what the result changed in the table, for the club `season_facts` picks: "matchday" with the rank once the whole matchday is played, "provisional" with
    # the rank this result alone gives; {o_*} are ordinal suffixes filled in code
    "matchday": {
        "move": {"en": "{club} move from {rb}{o_rb} to {ra}{o_ra} once the matchday is played, and their chance of going down goes from {cb}% to {ca}% with this result.",
                 "fr": "Après la journée, {club} est {ra}{o_ra} ({rb}{o_rb} avant le match), et ce résultat fait passer son risque de descente de {cb} % à {ca} %.",
                 "es": "Tras la jornada, el {club} queda {ra}{o_ra} ({rb}{o_rb} antes del partido), y este resultado lleva su riesgo de descenso del {cb}% al {ca}%."},
        "stay": {"en": "{club} stay {ra}{o_ra} once the matchday is played, and their chance of going down goes from {cb}% to {ca}% with this result.",
                 "fr": "Après la journée, {club} reste {ra}{o_ra}, et ce résultat fait passer son risque de descente de {cb} % à {ca} %.",
                 "es": "Tras la jornada, el {club} sigue {ra}{o_ra}, y este resultado lleva su riesgo de descenso del {cb}% al {ca}%."}},
    "provisional": {
        "move": {"en": "With this result, {club} are provisionally {ra}{o_ra} ({rb}{o_rb} before the match), and their chance of going down goes from {cb}% to {ca}%.",
                 "fr": "Avec ce résultat, {club} est provisoirement {ra}{o_ra} ({rb}{o_rb} avant le match), et son risque de descente passe de {cb} % à {ca} %.",
                 "es": "Con este resultado, el {club} es provisionalmente {ra}{o_ra} ({rb}{o_rb} antes del partido), y su riesgo de descenso pasa del {cb}% al {ca}%."},
        "stay": {"en": "With this result, {club} are provisionally still {ra}{o_ra}, and their chance of going down goes from {cb}% to {ca}%.",
                 "fr": "Avec ce résultat, {club} reste provisoirement {ra}{o_ra}, et son risque de descente passe de {cb} % à {ca} %.",
                 "es": "Con este resultado, el {club} sigue provisionalmente {ra}{o_ra}, y su riesgo de descenso pasa del {cb}% al {ca}%."}},
}


def ordinal_suffix(lang: str, n: int) -> str:
    """The suffix of a rank: 14th, 14e (1er), 14.º."""
    if lang == "fr":
        return "er" if n == 1 else "e"
    if lang == "es":
        return ".º"
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def season_facts(storyline: dict, analysis: dict, facts: dict, meanings: dict, roles: dict) -> None:
    """Adds what the result changed in the table (`storyline.post_match`: rank and relegation chance before and after) to the recap facts, and picks the club the season sentence is about: among the clubs whose chance of going down moved by 3 points or more, the winner first, else the one whose chance moved the most. League table points stay out (points on air are only a gap between two chances). When the rank after the whole matchday is known, the rank and the zone with this result alone stay out too, and the zone is told for that rank."""
    from worker.storyline import ZONES, in_zone, post_match, slug as club_slug

    score = analysis["score"]
    extra, extra_meanings = post_match(storyline, score["home"], score["away"])
    extra.pop("final_score", None)
    best = None
    winner = analysis["teams"].get(analysis["winner"]) if analysis.get("winner") else None
    for club, event in (storyline.get("focus") or {}).items():
        s, zone = club_slug(club), ZONES.get(event)
        full = extra.get(f"{s}_rank_after_matchday")
        if isinstance(full, int) and f"{s}_in_{zone}_after" in extra:
            extra[f"{s}_in_{zone}_after_matchday"] = in_zone(event, full)
            extra_meanings[f"{s}_in_{zone}_after_matchday"] = f"whether {club} are in the {zone.replace('_', ' ')} after the whole matchday"
        change = extra.get(f"{s}_{event}_change_pts")
        if event != "relegation" or not isinstance(change, int) or change < 3:
            continue
        rank = (club == winner, change)  # the winner's season first: it is the story of the match
        if best is None or rank > best[1]:
            best = (club, rank)
    kept = {k: v for k, v in extra.items() if not league_points_field(k) and f"{k}_matchday" not in extra}
    facts |= {f"season_{k}": v for k, v in kept.items()}
    meanings |= {f"season_{k}": v for k, v in extra_meanings.items() if k in kept}
    if best:
        roles["season_club"], roles["season_slug"] = best[0], club_slug(best[0])


def recap_cosmos(match_id: str, writer=None, fan_side: str | None = None) -> dict:
    """Reads the cards and the match state from Cosmos DB, writes the recap back next to them (`type: "recap"`, id `recap-<match_id>`)."""
    from worker.azure_io import cosmos_from_env

    container = cosmos_from_env().cards
    docs = list(container.query_items("SELECT * FROM c WHERE c.match_id = @m", parameters=[{"name": "@m", "value": match_id}], partition_key=match_id))
    cards = [d for d in docs if d.get("type") == "card"]
    state = next(d for d in docs if d.get("type") == "match_state")
    storyline = next((d for d in docs if d.get("type") == "storyline"), None)
    recap = build_recap(cards, state, writer, fan_side=fan_side, storyline=storyline)
    container.upsert_item(recap)
    return recap


def main() -> None:
    parser = argparse.ArgumentParser(description="Post-match recap and audience previews from the approved cards of a finished match.")
    parser.add_argument("desk", type=Path, nargs="?", help="desk directory of the match (data/desk/<match_id>), with cards.json and state.json")
    parser.add_argument("--cosmos", metavar="MATCH_ID", help="read and write Cosmos DB instead of a desk directory")
    parser.add_argument("--writer", choices=("template", "foundry"), default="template")
    parser.add_argument("--fan-team", choices=("home", "away"), help="side the fan text speaks for (default: the winner, or home in a draw)")
    parser.add_argument("--out", type=Path, help="where to write the recap (default: recap.json in the desk directory)")
    args = parser.parse_args()
    writer = None
    if args.writer == "foundry" or args.cosmos:
        from generator.publish import service_bus_settings

        service_bus_settings()  # loads .env and the azd environment (Foundry and Cosmos DB settings)
    if args.writer == "foundry":
        writer = RecapWriter()
    if args.cosmos:
        recap = recap_cosmos(args.cosmos, writer, args.fan_team)
    else:
        if args.desk is None:
            parser.error("give a desk directory, or --cosmos MATCH_ID")
        cards, state = load_desk(args.desk)
        story_path = args.desk / "storyline.json"
        storyline = json.loads(story_path.read_text(encoding="utf-8")) if story_path.exists() else None
        recap = build_recap(cards, state, writer, fan_side=args.fan_team, storyline=storyline)
    if not recap["final"]:
        print("warning: the match is not over, the recap covers it so far")
    out = args.out or (args.desk / "recap.json" if args.desk else None)
    text = json.dumps(recap, indent=1, ensure_ascii=False)
    if out:
        out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
