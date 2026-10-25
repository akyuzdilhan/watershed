"""The Writer and the Verifier: from a Spotter card (or an insight) to on-air text and a commentary duo in several languages, every number checked.

The code computes every number and sends it under an explicit field name (`card_facts`, `insight_facts`); the model only writes, and lists in `claims` each number it used with the field it comes from. The Verifier checks those claims, rejects any number nobody claimed, a percentage taken from a field that is not one, and flags any name that is not in the facts. It also applies broadcast style rules per language (`LANGUAGE_RULES`, `STRUCTURAL_RULES`): an error fails the draft, a warning is only reported. The duo lines (`duo` field, personas in `worker.duo`) go through the same checks, line by line. Across a match, a `PhraseMemory` remembers what was already said: an identical headline fails, a reused opening, phrase or tic is a warning. A failed draft gets one retry, told what was rejected, then fixed templates take over (many variants, the least used one picked deterministically), so a card is never blocked by the model.

"Points" has one meaning on screen: a change in a team's chance of winning. The card ranking value `value_pts` is never sent to the model nor written. A goal that hardly moves the result (under 2 points) is written as a milestone, with no probability (`low_stake`).

The model runs on Microsoft Foundry through Agent Framework (`FoundryWriter`, a function tool `get_moment`); `TemplateWriter` needs no Azure and serves the tests and the fallback.
"""

import inspect
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from worker.duo import (ALLOWED_NAMES, CARDINALS, CONDITIONS, EYE, HEART, LINES, NB, ORDINALS, PERSONAS, SMALL_MARGIN_PTS, TILT_PTS, PhraseMemory, count_words, fill, fr_de,
                        fr_typography, persona_brief, placeholders, rotated, stable)
from worker.feedback import skeleton

LANGUAGES = ("en", "fr", "es")
MACHINE_TRANSLATED = {"es"}  # shown on air with a "machine-translated" label
LOW_STAKE_PTS = 2  # a goal that moves its team's chance of winning by less than this is written without a probability

WRITER_INSTRUCTIONS = """You write live football broadcast cards: a headline and one sentence, "why", shown on screen during the match like a professional TV graphic.

Facts: call the get_moment tool with the card_id to get the facts and the meaning of each field. Use ONLY those facts. Never invent a statistic, name or fact. If something you are asked for is not in the facts, write "not available" for it.

Numbers. Each number has a field name that says what it means: never use a number with another meaning. For example, a win probability "if goal" is a chance of winning, never a chance of scoring. Do not compute any new number. Write every number exactly as given: whole numbers, no sign (never +24), no decimals.
- Fields ending in _pct are percentages: write them with a % sign (34%, never 0.34).
- Fields ending in _pts are percentage points: always write the word points (French: points, Spanish: puntos) right after the number, never a % sign.

Content. Explain why the moment matters through the change in win probability, and credit the action that caused it (a save, not the shot it stopped). Every sentence that gives a win probability names the team it belongs to: rovers_wp_after_pct is the chance of Rovers. Never write "win chance", "the chances" or "les chances" without the team.
- Headline: at most 8 words, names the player or the team. No minute and no number at the start: the graphic already shows the minute and the score.
- Why: one sentence, at most 25 words.
- A red card names the player in the headline or the why. Say second yellow only when card_kind is second_yellow, and straight red only when card_kind is straight_red.
- Same meaning in every language, each written natively as a broadcaster of that country would say it, not translated word for word.

Language rules.
- en: plain broadcast English. Say "Rovers go from 30% to 54% to win", "worth 13 points". No jargon: no "+24-point swing", no "win chance".
- fr: French TV style. Club names without article: "Rovers", "pour United", "contre Rovers"; never "l'United", "le Rovers", "d'United", "du Rovers", "aux Rovers". If an article or "d'" would fall before a club name, make the club the subject of the sentence. No English words: never "swing" or "momentum"; say "points", "points de probabilité" or "bascule" ("soit 24 points de mieux"). Put a space before %: "34 %".
- es: Spanish broadcast style. Use the article before a club name as Spanish commentators do ("el Rovers", "del United", "al Town"). Say "opciones de ganar" and "puntos". Never "swing" or "momentum", never "en puntos de diferencia".

Good and bad "why" sentences (a goal taking Rovers from 30% to 54%, swing_pts 24):
- en good: "Rovers go from 30% to 54% to win, up 24 points." Bad: "Win chance jumped: a +24-point swing." (no team, sign, jargon)
- fr good: "Rovers passe de 30 % à 54 % de chances de gagner, soit 24 points de mieux." Bad: "Le but fait bondir les chances de l'United : swing de 24 points." (whose chances, article, anglicism)
- es good: "El Rovers pasa del 30% al 54% de opciones de ganar: 24 puntos más." Bad: "Swing para el Rovers con un +24 en puntos de diferencia." (anglicism, sign, clumsy)

Reply with valid JSON only, no other text: a JSON array with one object per requested language:
{"headline": string (max 8 words), "why": string (max 25 words), "lang": string, "claims": [{"value": number, "source_field": string}]}
claims lists every number written in headline or why, with the field it comes from.
"""

# The worked example of the instructions, kept as data so that a test can run it through the Verifier.
EXAMPLE_FACTS = {"minute": "81'", "score_before": "City 0-0 Town", "event": "Town goalkeeper save", "goalkeeper": "Ari Holt",
                 "town_wp_after_save_pct": 22, "town_wp_if_goal_pct": 9, "save_value_pts": 13}
_EXAMPLE_CLAIMS = [{"value": 22, "source_field": "town_wp_after_save_pct"}, {"value": 9, "source_field": "town_wp_if_goal_pct"}, {"value": 13, "source_field": "save_value_pts"}]
EXAMPLE_REPLY = [
    {"headline": "Ari Holt keeps Town alive", "why": "The save kept Town at 22% to win instead of 9% if it had gone in: worth 13 points.", "lang": "en", "claims": _EXAMPLE_CLAIMS},
    {"headline": "Ari Holt maintient Town en vie", "why": "Town reste à 22 % de chances de gagner, contre 9 % si le tir était entré : l'arrêt vaut 13 points.", "lang": "fr", "claims": _EXAMPLE_CLAIMS},
    {"headline": "Ari Holt mantiene vivo al Town", "why": "La parada deja al Town con un 22% de opciones de ganar, frente al 9% si hubiera entrado: vale 13 puntos.", "lang": "es", "claims": _EXAMPLE_CLAIMS},
]
WRITER_INSTRUCTIONS += (f"\nExample for another moment, facts {json.dumps(EXAMPLE_FACTS, ensure_ascii=False)}, languages [\"en\", \"fr\", \"es\"]:\n"
                        + json.dumps(EXAMPLE_REPLY, ensure_ascii=False))

EXAMPLE_DUO = [
    {"speaker": "wren", "lang": "en", "line": "What a save! Ari Holt, you have just kept Town alive!", "claims": []},
    {"speaker": "joss", "lang": "en", "line": "Town stay on 22% to win instead of 9% had it gone in: worth 13 points.", "claims": _EXAMPLE_CLAIMS},
    {"speaker": "wren", "lang": "fr", "line": "Quel arrêt ! Ari Holt garde Town en vie !", "claims": []},
    {"speaker": "joss", "lang": "fr", "line": "Town reste à 22 % de chances de gagner, contre 9 % si le tir était entré : l'arrêt vaut 13 points.", "claims": _EXAMPLE_CLAIMS},
    {"speaker": "wren", "lang": "es", "line": "¡Qué parada! ¡Ari Holt mantiene vivo al Town!", "claims": []},
    {"speaker": "joss", "lang": "es", "line": "El Town sigue con un 22% de opciones de ganar, frente al 9% si hubiera entrado: 13 puntos.", "claims": _EXAMPLE_CLAIMS},
]

WRITER_INSTRUCTIONS += f"""

Points. The word points (French points, Spanish puntos) has one meaning: a gap between two chances, in percentage points, from a field ending in _pts. Never write any other value as points, and never with a verb of league points (take, prendre, gagner, ganar, sumar): "Rovers take 24 points" sounds like the league table.

A goal with no stake. When the facts have "stake": "low", the goal hardly changes the result: there is no probability in the facts. Write it as a milestone or a story line from team_goals_after and scorer_match_goals ("Rovers make it 5", "a 4th for Lightfoot"), with no probability and no points.

Insights and storyline. When the request has "document": "insight" or "storyline", the facts compare the match with the league's history: the headline states the fact and who did it, the why says why it is remarkable, with numbers only from the facts. A rarity is told in seasons, for the team or the player ("Rovers' first comeback from two goals down since 2019/20"); never a count of matches, a league-wide rate ("one match in 101", "about 3 a season"), a ratio ("228 times rarer") or bits: those stay with the producer.

Variety. The request may list, per language, the headlines, openings, verbs and duo lines already used in this match ("avoid"). Never repeat a headline or a duo line; do not start a sentence or build a headline like those; vary the verbs. Each match has its story: tell it from the facts (goal_situation, score_after, scorer_match_goals), never with the same words twice.

Commentary duo. When the request has "duo": true, also write what two fictional commentators say on air about the moment. Never name or imitate a real commentator or pundit; keep it broadcast-safe, never insulting.
{persona_brief(list(LANGUAGES))}
- 2 to 4 lines per requested language, alternating speakers, usually wren first: wren lives the moment and the story, joss brings one number or the detail nobody noticed, then they answer each other.
- Spoken, not written: one or two short sentences per line, at most 25 words, no headline style, and not a copy of the headline or the why.
- Same rules as the card for numbers, units, club names and each language's style: every number in a line is listed in that line's claims, and a line that gives a win probability names the team.
- Only a goal by the home team (team_at_home true) makes the stadium roar or wake up; for an away goal, speak of the away fans or the bench.
- The words match the size of the number: "small margins" only for a save worth less than {SMALL_MARGIN_PTS} points, "the whole match just tilted" only for a change of more than {TILT_PTS} points.
- When the facts have previous_scorer, the feed has credited the goal to another player: the duo announces the change of scorer, not the goal again.
- A tic is said at most once per match: "tics_used" in the request lists those already said.
Then reply with a JSON object instead of the array: {{"texts": [the array of cards], "duo": [{{"speaker": "wren" or "joss", "lang": string, "line": string, "claims": [{{"value": number, "source_field": string}}]}}]}}
Duo example for the moment above: {json.dumps(EXAMPLE_DUO, ensure_ascii=False)}
"""


def shown(pct: int) -> int:
    """A win probability on air: never 0% or 100% before the final whistle."""
    return min(max(pct, 1), 99)


def low_stake(card: dict) -> bool:
    """A goal that moves its team's chance of winning by less than LOW_STAKE_PTS points: written as a milestone, low priority on air."""
    f = card.get("facts", {})
    return card.get("moment") == "goal" and "team_wp_after_pct" in f and abs(shown(f["team_wp_after_pct"]) - shown(f["team_wp_before_pct"])) < LOW_STAKE_PTS


def goal_situation(card: dict) -> str:
    sb, team = card["score_before"], card["team"]
    diff = sb[team] - sb["away" if team == "home" else "home"]
    if diff < -1:
        return "pulls_one_back"
    if diff == -1:
        return "equaliser"
    if diff == 0:
        return "opener" if sb["home"] == sb["away"] == 0 else "go_ahead"
    return "extends_lead"


SITUATION_MEANINGS = {"opener": "the first goal of the match", "equaliser": "the goal levels the score", "go_ahead": "the goal puts the team ahead from level",
                      "extends_lead": "the team was already ahead", "pulls_one_back": "the team still trails after the goal"}


def card_facts(card: dict, teams: dict[str, str]) -> tuple[dict, dict]:
    """Facts for the Writer, with explicit names, and what each field means."""
    team, name = card["team"], card["team_name"]
    other = teams["away" if team == "home" else "home"]
    slug = name.lower()
    sb = card["score_before"]
    score = f"{teams['home']} {sb['home']}-{sb['away']} {teams['away']}"
    f = card["facts"]
    facts: dict[str, Any] = {"minute": card["minute_label"], "score_before": score, "team_at_home": team == "home"}
    meanings = {"minute": "match minute of the moment", "score_before": "score just before the moment",
                "team_at_home": f"true if {name} play at home: only a home goal makes the stadium roar or wake up"}
    player = card["player"]["name"]
    if card["moment"] == "goal":
        after = dict(sb)
        after[team] += 1
        facts |= {"event": f"{name} goal", "scorer": player, "score_after": f"{teams['home']} {after['home']}-{after['away']} {teams['away']}",
                  "goal_situation": goal_situation(card), "team_goals_after": after[team]}
        meanings |= {"scorer": "player who scored", "score_after": "score after the goal", "goal_situation": "what the goal does to the score: " + "; ".join(f"{k}: {v}" for k, v in SITUATION_MEANINGS.items()),
                     "team_goals_after": f"goals {name} have scored in this match, this one included"}
        if isinstance(card.get("scorer_match_goals"), int):
            facts["scorer_match_goals"] = card["scorer_match_goals"]
            meanings["scorer_match_goals"] = f"goals {player} has scored in this match, this one included"
        if card.get("previous_player") and card.get("previous_player") != player:
            facts["previous_scorer"] = card["previous_player"]
            meanings["previous_scorer"] = f"player the goal was first credited to; the feed has since credited it to {player}: tell the change of scorer"
        if low_stake(card):
            facts["stake"] = "low"
            meanings["stake"] = "the goal hardly changes the result: write it as a milestone or a story line, with no probability and no points"
        else:
            facts |= {f"{slug}_wp_before_pct": shown(f["team_wp_before_pct"]), f"{slug}_wp_after_pct": shown(f["team_wp_after_pct"])}
            facts["swing_pts"] = facts[f"{slug}_wp_after_pct"] - facts[f"{slug}_wp_before_pct"]
            meanings |= {f"{slug}_wp_before_pct": f"{name}'s chance of winning the match just before the goal, in %",
                         f"{slug}_wp_after_pct": f"{name}'s chance of winning the match just after the goal, in %",
                         "swing_pts": f"change in {name}'s chance of winning caused by the goal, in percentage points"}
    elif card["moment"] == "save":
        facts |= {"event": f"{name} goalkeeper save", "goalkeeper": player, "shot_xg_pct": f["xg_pct"],
                  f"{slug}_wp_after_save_pct": shown(f["team_wp_before_pct"]), f"{slug}_wp_if_goal_pct": shown(f["team_wp_if_goal_pct"])}
        facts["save_value_pts"] = facts[f"{slug}_wp_after_save_pct"] - facts[f"{slug}_wp_if_goal_pct"]
        meanings |= {"goalkeeper": f"{name}'s goalkeeper, who made the save", "shot_xg_pct": f"chance the {other} shot had of being a goal (expected goals), in %",
                     f"{slug}_wp_after_save_pct": f"{name}'s chance of winning the match after the save, in %",
                     f"{slug}_wp_if_goal_pct": f"{name}'s chance of winning the match if the shot had gone in, in %",
                     "save_value_pts": f"what the save was worth: {slug}_wp_after_save_pct minus {slug}_wp_if_goal_pct, in percentage points"}
    elif card["moment"] == "red_card":
        facts |= {"event": f"{name} player sent off", "player_sent_off": player,
                  f"{slug}_wp_before_pct": shown(f["team_wp_before_pct"]), f"{slug}_wp_after_pct": shown(f["team_wp_after_pct"])}
        meanings |= {"player_sent_off": "player shown the red card, to be named in the headline or the why", f"{slug}_wp_before_pct": f"{name}'s chance of winning the match before the red card, in %",
                     f"{slug}_wp_after_pct": f"{name}'s chance of winning the match with ten men, in %"}
        if card.get("card_kind") in CARD_KINDS:
            facts["card_kind"] = CARD_KINDS[card["card_kind"]]
            meanings["card_kind"] = "how the player was sent off: " + "; ".join(f"{k}: {v}" for k, v in CARD_KIND_MEANINGS.items())
    else:  # a chance that did not go in
        facts |= {"event": f"{name} chance missed", "shooter": player, "shot_xg_pct": f["xg_pct"],
                  f"{slug}_wp_before_pct": shown(f["team_wp_before_pct"]), f"{slug}_wp_if_goal_pct": shown(f["team_wp_if_goal_pct"])}
        meanings |= {"shooter": "player who took the shot", "shot_xg_pct": "chance the shot had of being a goal (expected goals), in %",
                     f"{slug}_wp_before_pct": f"{name}'s chance of winning the match, in %",
                     f"{slug}_wp_if_goal_pct": f"{name}'s chance of winning the match if the shot had gone in, in %"}
    season = card.get("season_context") or {}
    if season.get("facts"):  # what the score means for the season (worker.storyline.live_context), prefixed so it never collides with the match facts
        facts |= {f"season_{k}": v for k, v in season["facts"].items()}
        meanings |= {f"season_{k}": v for k, v in season.get("meanings", {}).items()}
    return facts, meanings


CARD_KINDS = {"second_yellow": "second_yellow", "red": "straight_red"}  # card["card_kind"], set by the worker -> the card_kind fact
CARD_KIND_MEANINGS = {"second_yellow": "a second booking in the match, so a red card", "straight_red": "a straight red card"}


def league_points_field(name: str) -> bool:
    """A field of league table points ("rovers_points_if_win", "points_gap"): never sent to the model, since "points" on air is only a gap between two chances."""
    return "points" in name.split("_")


OFF_AIR_FIELDS = ("one_in", "surprisal_bits", "history_opportunities", "history_per_season", "times_rarer_than_runner_up", "runner_up_surprisal_bits",
                  "top_value", "runner_up", "runner_up_value")  # rarity measures and desk ranking: the producer sees them in the console, the audience never


def on_air(facts: dict, meanings: dict) -> tuple[dict, dict]:
    """The facts the Writer may write from: an insight's facts without the league-wide rates, match counts, ratios, bits and desk values, and without league table points."""
    kept = {k: v for k, v in facts.items() if k not in OFF_AIR_FIELDS and not league_points_field(k)}
    return kept, {k: v for k, v in meanings.items() if k in kept}


TIGHT_OFFSIDE_CM = 30  # a player at 7.5 m/s covers 30 cm between two frames at 25 Hz: below that, a number would be false precision


def offside_call(margin_cm: int) -> str:
    return "tight" if margin_cm <= TIGHT_OFFSIDE_CM else "clear"


def ruling_facts(card: dict, teams: dict[str, str]) -> tuple[dict, dict]:
    """A goal card's facts plus why VAR ruled it out: the reason, and for offside whether it was tight or clear (the margin in centimetres stays on the card for the console, never on air)."""
    facts, meanings = card_facts(card, teams)
    ruled = card.get("ruled_out") or {}
    facts["ruled_out_reason"] = ruled.get("reason") or "other"
    meanings["ruled_out_reason"] = "why VAR ruled the goal out: offside, handball or other"
    if isinstance(ruled.get("margin_cm"), int):
        facts["offside_call"] = offside_call(ruled["margin_cm"])
        meanings["offside_call"] = "how close the offside was: tight (within the distance a sprinting player covers between two frames) or clear"
    return facts, meanings


SUBJECT_FIELDS = ("player", "player_name", "scorer", "subject", "team", "club", "team_name")


def insight_facts(doc: dict, teams: dict[str, str]) -> tuple[dict, dict]:
    """Facts of an insight (`headline_facts`) or of the storyline (`facts`), with the two clubs and the minute added."""
    facts = dict(doc.get("headline_facts") or doc.get("facts") or {})
    meanings = dict(doc.get("field_meanings") or {})
    facts.setdefault("home_team", teams.get("home"))
    facts.setdefault("away_team", teams.get("away"))
    meanings.setdefault("home_team", "home club of the match")
    meanings.setdefault("away_team", "away club of the match")
    if doc.get("minute_label"):
        facts.setdefault("minute", doc["minute_label"])
        meanings.setdefault("minute", "match minute of the fact")
    facts = {k: v for k, v in facts.items() if v is not None}
    return facts, meanings


# --- Verifier -------------------------------------------------------------------------------------

# A number: thousands groups (18,620, 18 620, 18.620) read as one integer, else an optional decimal part.
NUMBER = re.compile(r"\d{1,3}(?:[ ,.]\d{3})+(?![\d.,]\d)|\d+(?:[.,]\d+)?")


def to_number(s: str) -> float:
    if re.fullmatch(r"\d{1,3}(?:[ ,.]\d{3})+", s):
        return float(re.sub(r"[ ,.]", "", s))
    return float(s.replace(",", "."))


def numbers_in(text: str) -> list[tuple[float, bool]]:
    text = normalise(text)
    return [(to_number(m.group(0)), bool(re.match(r"\s?%", text[m.end():]))) for m in NUMBER.finditer(text)]


def normalise(text: str) -> str:
    return text.replace("\xa0", " ").replace("\u202f", " ")


def claim_ok(claim: Any, facts: dict) -> bool:
    if not isinstance(claim, dict) or not isinstance(claim.get("value"), (int, float)):
        return False
    src = claim.get("source_field")
    value = facts.get(src)
    if isinstance(value, str):  # a score or a minute: the number must appear in it
        return claim["value"] in [float(n) for n in re.findall(r"\d+", value)]
    return isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) == float(claim["value"])


def names_ok(text: str, facts: dict, extra: set[str] | frozenset = frozenset()) -> list[str]:
    """Capitalised words that are neither in the facts nor at the start of a sentence: possible invented names."""
    allowed = " ".join(str(v) for v in facts.values())
    words = re.findall(r"(?<![.!?:]\s)(?<![¡¿])(?<!^)\b([A-ZÀ-Ý][\wÀ-ÿ'-]+)", text)
    words = [re.sub(r"['’]s?$", "", w) for w in words]  # Lightfoot's
    return sorted({w for w in words if w not in allowed and w not in extra and w != "VAR"})


# --- style rules ----------------------------------------------------------------------------------
# Every rule is data: a code, a severity and a pattern. An "error" fails the draft (one retry, then the template), a "warning" is
# only reported. In a pattern, {team} stands for each club name of the match. The checks that need the claims (which team a
# probability belongs to, the unit of a number) are listed in STRUCTURAL_RULES and implemented in style_issues.

FR_ARTICLE = r"(?i:\b(?:l['’]\s?|d['’]\s?|(?:le|la|les|du|des|au|aux)\s+))"

LANGUAGE_RULES: dict[str, list[tuple[str, str, str]]] = {
    "en": [
        ("jargon", "warning", r"(?i)\bwin chances?\b"),
        ("league_points_verb", "error", r"(?i)\b(?:take|takes|took|taken|taking|gain|gains|gained|gaining)\s+(?:\w+\s+)?\d+\s+points?\b"),  # "points" with a verb of league points
        ("league_rate", "error", r"(?i)\b(?:one|1|once)\s+(?:(?:match|game)\s+)?(?:in|out of)\s+(?:every\s+)?\d+(?!\d|\s+seasons?\b)"  # "not once in 50 seasons" is a landmark, not a rate
                                 r"|\b(?:\d+(?:[.,]\d+)?|once|twice|\w+\s+times)\s+(?:a|per|each|every)\s+season\b"),
        ("league_rate", "error", r"(?i)\b\d+(?:[.,]\d+)?\s+times\s+(?:rarer|more\s+rare|less\s+likely)\b|\b\d+(?:[.,]\d+)?\s+bits?\b|\b\d{1,3}(?:[ ,.]\d{3})+\s+(?:matches|games)\b|\b\d{3,}\s+(?:matches|games)\b"),
    ],
    "fr": [
        ("forbidden_word", "error", r"(?i)\bswing\w*"),
        ("forbidden_word", "error", r"(?i)\bmomentum\b"),
        ("forbidden_word", "error", r"(?i)\bgame[- ]?changer\b"),
        ("article_before_team", "error", FR_ARTICLE + r"{team}\b"),
        ("anglicism", "warning", r"(?i)\b(?:goal|save|clean sheet|turnover|win)\b"),
        ("percent_without_space", "warning", r"\d%"),
        ("league_points_verb", "error", r"(?i)\b(?:prendre|prend|prennent|pris|prenait|gagner|gagne|gagnent|gagné|gagnés|gagnait)\s+(?:\w+\s+)?\d+\s+points?\b"),
        ("league_rate", "error", r"(?i)\b(?:un|1)\s+match\s+sur\s+\d|\bune\s+fois\s+sur\s+\d|\b(?:\d+(?:[.,]\d+)?\s+|\w+\s+fois\s+)par\s+saison\b|\b\d+(?:[.,]\d+)?\s+fois\s+plus\s+rare"),
        ("league_rate", "error", r"(?i)\b\d+(?:[.,]\d+)?\s+bits?\b|\b\d{1,3}(?:[ ,.]\d{3})+\s+matchs\b|\b\d{3,}\s+matchs\b"),
    ],
    "es": [
        ("forbidden_word", "error", r"(?i)\bswing\w*"),
        ("forbidden_word", "error", r"(?i)\bmomentum\b"),
        ("forbidden_word", "error", r"(?i)\bgame[- ]?changer\b"),
        ("clumsy_phrase", "warning", r"(?i)\ben puntos de diferencia\b"),
        ("anglicism", "warning", r"(?i)\b(?:goal|save|clean sheet|win)\b"),
        ("league_points_verb", "error", r"(?i)\b(?:ganar|gana|ganan|ganó|ganado|ganaba|sumar|suma|suman|sumó|sumado)\s+(?:\w+\s+)?\d+\s+puntos?\b"),
        ("league_rate", "error", r"(?i)\b(?:un|1)\s+partido\s+de\s+cada\s+\d|\b(?:una\s+vez|uno)\s+(?:de\s+)?cada\s+\d|\b(?:\d+(?:[.,]\d+)?\s+|\w+\s+(?:vez|veces)\s+)(?:por|cada|a\s+la)\s+temporada\b"
                                 r"|\b\d+(?:[.,]\d+)?\s+veces\s+más\s+rar[oa]"),
        ("league_rate", "error", r"(?i)\b\d+(?:[.,]\d+)?\s+bits?\b|\b\d{1,3}(?:[ ,.]\d{3})+\s+partidos\b|\b\d{3,}\s+partidos\b"),
    ],
}
# What a red card text may say only when the card_kind fact says it.
CARD_KIND_WORDS = {
    "second_yellow": {"en": r"(?i)\bsecond (?:yellow|booking)|\bbooked (?:twice|again)\b|\btwo bookings\b",
                      "fr": r"(?i)\b(?:second|deuxième) (?:carton jaune|jaune|avertissement)|\baverti (?:deux fois|une deuxième fois)|\bdouble avertissement",
                      "es": r"(?i)\bsegunda (?:tarjeta )?amarilla|\bdoble amarilla|\bdos amarillas"},
    "straight_red": {"en": r"(?i)\bstraight red", "fr": r"(?i)\b(?:carton )?rouge direct", "es": r"(?i)\b(?:tarjeta )?roja directa"},
}

# Words that speak of a win probability: a sentence that uses one must name a club.
PROBABILITY_WORDS = {
    "en": r"(?i)\b(?:win(?:ning)? (?:chances?|probability|odds)|chances? of winning|to win)\b",
    "fr": r"(?i)\b(?:chances?|probabilités?)\b",
    "es": r"(?i)\b(?:opciones|probabilidad(?:es)?|posibilidades)\b",
}
POINT_WORDS = {"en": r"points?", "fr": r"points?", "es": r"puntos?"}
# Counting words (3 to 9, third to tenth) a text may use only when a whole-number fact has that value: "a fifth goal" needs team_goals_after 5.
COUNT_WORDS = {lang: {w: i + 1 for i, w in enumerate(ORDINALS[lang]) if i >= 2} | {w: i + 1 for i, w in enumerate(CARDINALS[lang]) if 2 <= i <= 8} for lang in ORDINALS}
COUNT_WORDS["fr"].pop("neuf")  # also "new"

STRUCTURAL_RULES = {
    "signed_number": ("error", r"(?<![\w])[+±]\s?\d+"),  # "+24": say the direction with a verb
    "decimal_percent": ("error", r"\d+[.,]\d+\s?%"),  # "34.5%": percentages are whole numbers
    "headline_starts_with_number": ("error", r"^\W*\d"),  # "52' ...": the graphic already shows the minute
    "minute_in_headline": ("warning", r"(?i)\d+(?:\+\d+)?\s?['’′]|\bmin(?:ute|uto)?s?\b"),
    "probability_without_team": ("error", None),  # a probability word in a sentence that names no club
    "probability_number_without_team": ("error", None),  # a number from a <team>_wp_ field in a sentence without that team
    "points_unit_missing": ("error", None),  # a number from a _pts field not followed by the word for points
    "points_not_from_pts_field": ("error", None),  # "54 points" where 54 is a percentage
    "card_kind_unbacked": ("error", None),  # "second yellow" or "straight red" when the card_kind fact does not say so
    "player_not_named": ("error", None),  # a red card whose headline and why do not name the player (card texts only)
    "count_word_unbacked": ("warning", None),  # "fifth" when no whole-number fact is 5
    "too_long": ("warning", None),  # headline over 8 words or why over 25
}
HEADLINE_MAX_WORDS, WHY_MAX_WORDS = 8, 25


def teams_in(facts: dict) -> list[str]:
    """The two club names, read from the score line "Rovers 0-2 United" (or the home_team and away_team facts of an insight)."""
    found = re.match(r"^(.*\S)\s+\d+-\d+\s+(\S.*)$", str(facts.get("score_before", "")))
    if found:
        return [found.group(1), found.group(2)]
    return [str(facts[k]) for k in ("home_team", "away_team") if facts.get(k)]


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def mentions(sentence: str, team: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(team)}(?![\w])", sentence, re.I) is not None


def number_spans(text: str) -> list[tuple[float, int, int]]:
    return [(to_number(m.group(0)), m.start(), m.end()) for m in NUMBER.finditer(text)]


def style_issues(headline: str, why: str, lang: str, claims: list[dict], facts: dict) -> tuple[list[dict], list[dict]]:
    """Style checks of one draft: (errors, warnings), each issue {"rule", "text"}."""
    issues: list[tuple[str, str, str]] = []  # (severity, rule, matched text)
    headline, why = normalise(headline), normalise(why)
    body = f"{headline} {why}"
    teams = teams_in(facts)

    def add(rule: str, text: str, severity: str | None = None) -> None:
        issues.append((severity or STRUCTURAL_RULES[rule][0], rule, text))

    for rule, severity, pattern in LANGUAGE_RULES.get(lang, []):
        for p in ([pattern.replace("{team}", re.escape(t)) for t in teams] if "{team}" in pattern else [pattern]):
            for m in re.finditer(p, body):
                add(rule, m.group(0), severity)
    for rule in ("signed_number", "decimal_percent"):
        for m in re.finditer(STRUCTURAL_RULES[rule][1], body):
            add(rule, m.group(0))
    for rule in ("headline_starts_with_number", "minute_in_headline"):
        found = re.search(STRUCTURAL_RULES[rule][1], headline)
        if found:
            add(rule, headline)
    if len(headline.split()) > HEADLINE_MAX_WORDS or len(why.split()) > WHY_MAX_WORDS:
        add("too_long", f"{len(headline.split())} + {len(why.split())} words")
    counts = {v for v in facts.values() if isinstance(v, int) and not isinstance(v, bool)}
    for w in re.findall(r"[\wÀ-ÿ]+", body.lower()):
        if w in COUNT_WORDS.get(lang, {}) and COUNT_WORDS[lang][w] not in counts:
            add("count_word_unbacked", w)
    if "player_sent_off" in facts:
        for kind, words in CARD_KIND_WORDS.items():
            found = re.search(words[lang], body) if lang in words else None
            if found and facts.get("card_kind") != kind:
                add("card_kind_unbacked", found.group(0))

    wp_teams = {}  # value -> clubs whose win probability has that value
    pts_values = set()
    for c in claims:
        field_name = str(c.get("source_field", ""))
        if "_wp_" in field_name:
            slug = field_name.split("_wp_")[0]
            wp_teams.setdefault(float(c["value"]), set()).update(t for t in teams if t.lower() == slug)
        if field_name.endswith("_pts"):
            pts_values.add(float(c["value"]))
    for sentence in sentences(headline) + sentences(why):
        named = any(mentions(sentence, t) for t in teams)
        if lang in PROBABILITY_WORDS and not named and re.search(PROBABILITY_WORDS[lang], sentence):
            add("probability_without_team", sentence)
        for value, _, _ in number_spans(sentence):
            owners = wp_teams.get(value)
            if owners and not any(mentions(sentence, t) for t in owners):
                add("probability_number_without_team", sentence)
                break
    if lang in POINT_WORDS:
        point = re.compile(rf"(?i)^{POINT_WORDS[lang]}$")
        spans = number_spans(body)
        for value, _, end in spans:
            following = re.findall(r"[\wÀ-ÿ]+", body[end:end + 40])
            if following[:1] and point.match(following[0]) and value not in pts_values:
                add("points_not_from_pts_field", body[max(0, end - 6):end + 8].strip())
        for value in pts_values:
            ends = [end for v, _, end in spans if v == value]
            near = [any(point.match(w) for w in re.findall(r"[\wÀ-ÿ]+", body[end:end + 40])[:3]) for end in ends]
            if ends and not any(near):
                add("points_unit_missing", f"{value:g}")

    errors = [{"rule": r, "text": t} for s, r, t in issues if s == "error"]
    warnings = [{"rule": r, "text": t} for s, r, t in issues if s == "warning"]
    return errors, warnings


def check_numbers(body: str, claims: list, facts: dict) -> dict:
    """The number checks shared by card texts and duo lines."""
    good = [c for c in claims if claim_ok(c, facts)]
    unclaimed, wrong_unit = [], []
    for value, is_pct in numbers_in(body):
        matches = [c for c in good if float(c["value"]) == value]
        if not matches:
            unclaimed.append(value)
        elif is_pct and not any(c["source_field"].endswith("_pct") for c in matches):
            wrong_unit.append(value)
    return {"good": good, "bad_claims": [c for c in claims if not claim_ok(c, facts)], "unclaimed_numbers": unclaimed,
            "wrong_unit": wrong_unit + [v for v, _ in numbers_in(body) if 0 < v < 1]}


def verify(text: str, languages: list[str], facts: dict, style: bool = True) -> tuple[list[dict], dict]:
    """Checks a Writer reply. `style=False` keeps the number and name checks only, for texts that are not card headlines and whys (the post-match recap)."""
    report: dict[str, Any] = {"json_valid": True, "languages_ok": True, "cards": []}
    try:
        drafts = json.loads(text.strip())
    except json.JSONDecodeError:
        report["json_valid"] = False
        found = re.search(r"\[.*\]", text, re.S)
        try:
            drafts = json.loads(found.group(0)) if found else []
        except json.JSONDecodeError:
            drafts = []
    if not isinstance(drafts, list) or not all(isinstance(d, dict) for d in drafts):
        drafts = []
    report["languages_ok"] = sorted(str(d.get("lang")) for d in drafts) == sorted(languages)
    for d in drafts:
        body = f"{d.get('headline', '')} {d.get('why', '')}"
        claims = d.get("claims") if isinstance(d.get("claims"), list) else []
        nums = check_numbers(body, claims, facts)
        errors, warnings = style_issues(str(d.get("headline", "")), str(d.get("why", "")), str(d.get("lang")), nums["good"], facts) if style else ([], [])
        sent_off = str(facts.get("player_sent_off") or "")
        if style and sent_off and not any(n in body for n in (sent_off, sent_off.split()[-1])):
            errors.append({"rule": "player_not_named", "text": sent_off})
        entry = {
            "lang": d.get("lang"),
            "keys_ok": all(isinstance(d.get(k), str) for k in ("headline", "why", "lang")),
            "bad_claims": nums["bad_claims"],
            "unclaimed_numbers": nums["unclaimed_numbers"],
            "wrong_unit": nums["wrong_unit"],
            "unknown_names": sorted(set(names_ok(str(d.get("headline", "")), facts)) | set(names_ok(str(d.get("why", "")), facts))),  # each starts a sentence
            "length_ok": len(str(d.get("headline", "")).split()) <= HEADLINE_MAX_WORDS and len(str(d.get("why", "")).split()) <= WHY_MAX_WORDS,
            "style_errors": errors,
            "style_warnings": warnings,
        }
        entry["verified"] = entry["keys_ok"] and not entry["bad_claims"] and not entry["unclaimed_numbers"] and not entry["wrong_unit"] and not errors
        report["cards"].append(entry)
    report["verified"] = bool(drafts) and report["languages_ok"] and all(e["verified"] for e in report["cards"])
    return drafts, report


DUO_MIN_LINES, DUO_MAX_LINES, DUO_LINE_MAX_WORDS = 2, 4, 30


def verify_duo(lines: Any, languages: list[str], facts: dict) -> dict:
    """Checks duo lines: 2 to 4 per language, alternating known speakers, and each line like a card text (numbers claimed, units, style)."""
    lines = [l for l in lines if isinstance(l, dict)] if isinstance(lines, list) else []
    report: dict[str, Any] = {"languages": {}, "lines": []}
    for lang in languages:
        speakers = [l.get("speaker") for l in lines if l.get("lang") == lang]
        report["languages"][lang] = {"count": len(speakers), "count_ok": DUO_MIN_LINES <= len(speakers) <= DUO_MAX_LINES,
                                     "alternates": all(a != b for a, b in zip(speakers, speakers[1:])), "speakers_ok": all(s in PERSONAS for s in speakers)}
    for l in lines:
        text = str(l.get("line", ""))
        claims = l.get("claims") if isinstance(l.get("claims"), list) else []
        nums = check_numbers(text, claims, facts)
        errors, warnings = style_issues("", text, str(l.get("lang")), nums["good"], facts)
        warnings = [w for w in warnings if w["rule"] != "too_long"]
        if len(text.split()) > DUO_LINE_MAX_WORDS:
            warnings.append({"rule": "too_long", "text": f"{len(text.split())} words"})
        entry = {"lang": l.get("lang"), "speaker": l.get("speaker"), "keys_ok": isinstance(l.get("line"), str) and bool(text.strip()) and l.get("lang") in languages,
                 "bad_claims": nums["bad_claims"], "unclaimed_numbers": nums["unclaimed_numbers"], "wrong_unit": nums["wrong_unit"],
                 "unknown_names": names_ok(text, facts, ALLOWED_NAMES), "style_errors": errors, "style_warnings": warnings}
        entry["verified"] = entry["keys_ok"] and not entry["bad_claims"] and not entry["unclaimed_numbers"] and not entry["wrong_unit"] and not errors
        report["lines"].append(entry)
    report["verified"] = bool(lines) and all(v["count_ok"] and v["alternates"] and v["speakers_ok"] for v in report["languages"].values()) and all(e["verified"] for e in report["lines"])
    return report


def parse_reply(text: str) -> tuple[Any, Any, bool]:
    """(texts, duo, json_valid) of a reply: a JSON array of texts, or an object {"texts": [...], "duo": [...]}."""
    try:
        obj, valid = json.loads(text.strip()), True
    except (json.JSONDecodeError, AttributeError):
        obj, valid = None, False
        for pattern in (r"\{.*\}", r"\[.*\]"):
            found = re.search(pattern, text or "", re.S)
            try:
                obj = json.loads(found.group(0)) if found else None
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, (dict, list)):
                break
    if isinstance(obj, dict) and "texts" in obj:
        return obj.get("texts"), obj.get("duo"), valid
    return (obj if isinstance(obj, list) else []), None, valid


def verify_reply(text: str, languages: list[str], facts: dict, duo: bool = False, memory: PhraseMemory | None = None, doc_id: str = "") -> tuple[list[dict], list[dict], dict]:
    """Texts and duo of a reply, checked: the card checks, the duo checks when asked, and repetition against the rest of the match (a repeated headline fails the texts, a repeated duo line the duo)."""
    texts_raw, duo_raw, valid = parse_reply(text)
    texts, report = verify(json.dumps(texts_raw if isinstance(texts_raw, list) else [], ensure_ascii=False), languages, facts)
    report["json_valid"] = valid and report["json_valid"]
    lines = [l for l in duo_raw if isinstance(l, dict)] if isinstance(duo_raw, list) else []
    errors, warnings = memory.issues(doc_id, texts, lines if duo else []) if memory is not None else ([], [])
    report["repetition"] = {"errors": errors, "warnings": warnings}
    report["texts_verified"] = report["verified"] and report["json_valid"] and not [e for e in errors if e["rule"] != "repeated_line"]
    report["duo"] = verify_duo(lines, languages, facts) if duo else None
    if duo:
        report["duo"]["repeated_lines"] = [e for e in errors if e["rule"] == "repeated_line"]
        report["duo"]["verified"] = report["duo"]["verified"] and not report["duo"]["repeated_lines"]
    report["verified"] = report["texts_verified"] and (report["duo"]["verified"] if duo else True)
    return texts, lines, report


def feedback(report: dict) -> str:
    """What the Verifier rejected, for the retry."""
    lines = []
    if not report["json_valid"]:
        lines.append("the reply was not valid JSON")
    if not report["languages_ok"]:
        lines.append("the languages do not match the request")
    for e in report["cards"]:
        problems = [f"{k}: {e[k]}" for k in ("bad_claims", "unclaimed_numbers", "wrong_unit") if e[k]]
        problems += [f"{i['rule']}: {i['text']}" for i in e["style_errors"]]
        if problems:
            lines.append(f"{e['lang']}: " + "; ".join(problems))
    for i in (report.get("repetition") or {}).get("errors", []):
        what = "duo line already said" if i["rule"] == "repeated_line" else "headline already used"
        lines.append(f"{i['lang']}: {what} in this match: {i['text']}")
    duo = report.get("duo")
    if duo and not duo["verified"]:
        for lang, v in duo["languages"].items():
            if not (v["count_ok"] and v["alternates"] and v["speakers_ok"]):
                lines.append(f"duo {lang}: 2 to 4 lines, alternating wren and joss")
        for e in duo["lines"]:
            problems = [f"{k}: {e[k]}" for k in ("bad_claims", "unclaimed_numbers", "wrong_unit") if e[k]] + [f"{i['rule']}: {i['text']}" for i in e["style_errors"]]
            if problems or not e["keys_ok"]:
                lines.append(f"duo {e['lang']} {e['speaker']}: " + ("; ".join(problems) or "empty or unknown language"))
    return "The Verifier rejected the previous draft (" + " | ".join(lines)[:600] + "). Write it again following the rules."


# --- templates ------------------------------------------------------------------------------------
# Many variants per moment and language: the template writer picks, deterministically, the one least used in the match (headline never
# reused, fresh verb, fresh opening, fewest shared phrases). Every headline of a goal names the scorer; every "why" names the team.

TEMPLATES = {
    "goal": {
        "en": {"headlines": ["{scorer} scores for {team}", "{scorer} strikes for {team}", "{scorer} finds the net for {team}", "{scorer} delivers for {team}",
                             "{scorer} on target for {team}", "{scorer} beats the keeper", "{scorer} puts it away for {team}", "{scorer} fires {team} on"],
               "situations": {"opener": ["{scorer} opens the scoring", "{scorer} breaks the deadlock"], "equaliser": ["{scorer} levels for {team}", "{scorer} brings {team} level"],
                              "go_ahead": ["{scorer} puts {team} ahead", "{scorer} gives {team} the lead"], "extends_lead": ["{scorer} extends the {team} lead", "{scorer} stretches the {team} lead"],
                              "pulls_one_back": ["{scorer} pulls one back for {team}", "{scorer} gives {team} hope"]},
               "whys": ["{team} go from {before}% to {after}% to win, up {swing} points.", "That goal lifts {team} from {before}% to {after}% to win: {swing} points.",
                        "{team} now have {after}% to win, {swing} points more than before the goal.", "Worth {swing} points to {team}, who move from {before}% to {after}% to win.",
                        "{team} climb from {before}% to {after}% to win with that goal, up {swing} points.", "From {before}% to {after}% to win for {team}: the goal is worth {swing} points.",
                        "{team} are up to {after}% to win after that, a rise of {swing} points.", "{team} rise to {after}% to win, from {before}% before the goal."]},
        "fr": {"headlines": ["{scorer} marque pour {team}", "{scorer} frappe pour {team}", "{scorer} trouve le chemin des filets", "{scorer} fait mouche pour {team}",
                             "{scorer} ne tremble pas", "{scorer} trompe le gardien", "{scorer} conclut pour {team}", "{scorer} signe le but pour {team}"],
               "situations": {"opener": ["{scorer} ouvre le score", "{scorer} débloque le match"], "equaliser": ["{scorer} égalise pour {team}", "{scorer} remet {team} à hauteur"],
                              "go_ahead": ["{scorer} donne l'avantage à {team}", "{scorer} met {team} devant"], "extends_lead": ["{scorer} creuse l'écart pour {team}", "{scorer} enfonce le clou pour {team}"],
                              "pulls_one_back": ["{scorer} réduit l'écart pour {team}", "{scorer} relance {team}"]},
               "whys": ["{team} passe de {before} % à {after} % de chances de gagner, soit {swing} points de mieux.", "Ce but fait passer {team} de {before} % à {after} % de chances de gagner : {swing} points.",
                        "{team} a désormais {after} % de chances de gagner, {swing} points de plus qu'avant le but.", "Un bond de {swing} points pour {team}, qui passe de {before} % à {after} % de chances de gagner.",
                        "{team} grimpe de {before} % à {after} % de chances de gagner grâce à ce but, soit {swing} points.", "De {before} % à {after} % de chances de gagner pour {team} : ce but vaut {swing} points.",
                        "{team} monte à {after} % de chances de gagner, en hausse de {swing} points.", "{team} se hisse à {after} % de chances de gagner, contre {before} % avant le but."]},
        "es": {"headlines": ["Gol de {scorer} para el {team}", "{scorer} marca para el {team}", "{scorer} no perdona", "{scorer} define para el {team}",
                             "{scorer} bate al portero", "{scorer} acierta para el {team}", "{scorer} la manda a la red", "{scorer} firma el gol del {team}"],
               "situations": {"opener": ["{scorer} abre el marcador", "{scorer} estrena el marcador"], "equaliser": ["{scorer} empata para el {team}", "{scorer} iguala para el {team}"],
                              "go_ahead": ["{scorer} adelanta al {team}", "{scorer} pone por delante al {team}"], "extends_lead": ["{scorer} amplía la ventaja del {team}", "{scorer} agranda la ventaja del {team}"],
                              "pulls_one_back": ["{scorer} recorta para el {team}", "{scorer} da vida al {team}"]},
               "whys": ["El {team} pasa del {before}% al {after}% de opciones de ganar: {swing} puntos más.", "Ese gol lleva al {team} del {before}% al {after}% de opciones de ganar, {swing} puntos.",
                        "El {team} tiene ahora un {after}% de opciones de ganar, {swing} puntos más que antes del gol.", "Un salto de {swing} puntos para el {team}, que pasa del {before}% al {after}% de opciones de ganar.",
                        "El {team} sube del {before}% al {after}% de opciones de ganar con este gol: {swing} puntos.", "Del {before}% al {after}% de opciones de ganar para el {team}: el gol vale {swing} puntos.",
                        "El {team} llega al {after}% de opciones de ganar, {swing} puntos por encima.", "El {team} se va al {after}% de opciones de ganar, frente al {before}% de antes del gol."]},
    },
    "save": {
        "en": {"headlines": ["Big save from {player}", "{player} keeps it out", "{player} stands tall", "{player} denies the shot", "What a stop from {player}", "{player} to the rescue for {team}"],
               "whys": ["{team} stay on {before}% to win instead of {other}% had it gone in: the save is worth {value} points.",
                        "Had that gone in, {team} would be on {other}% to win; they stay on {before}%, {value} points kept.",
                        "The save keeps {team} on {before}% to win, against {other}% if it had gone in: worth {value} points.",
                        "Worth {value} points to {team}: {before}% to win rather than {other}% had it gone in.",
                        "{team} hold on to {before}% to win; a goal there would have dropped them to {other}%."]},
        "fr": {"headlines": ["{player} s'interpose", "{player} dit non", "{player} ferme la porte", "{player} sauve {team}", "{player} sort le grand jeu", "Parade décisive {de_player}"],
               "whys": ["{team} reste à {before} % de chances de gagner, contre {other} % si le tir était entré : l'arrêt vaut {value} points.",
                        "Si ça rentrait, {team} tombait à {other} % de chances de gagner ; {team} reste à {before} %, soit {value} points préservés.",
                        "L'arrêt garde {team} à {before} % de chances de gagner, au lieu de {other} % : il vaut {value} points.",
                        "Un arrêt à {value} points pour {team} : {before} % de chances de gagner au lieu de {other} %.",
                        "{team} s'accroche à ses {before} % de chances de gagner ; un but l'aurait fait tomber à {other} %."]},
        "es": {"headlines": ["Paradón de {player}", "{player} lo evita", "{player} dice que no", "{player} salva al {team}", "{player} cierra la puerta", "Intervención clave de {player}"],
               "whys": ["El {team} se mantiene en el {before}% de opciones de ganar, frente al {other}% si hubiera entrado: la parada vale {value} puntos.",
                        "Si entraba, el {team} caía al {other}% de opciones de ganar; sigue en el {before}%: {value} puntos salvados.",
                        "La parada mantiene al {team} en el {before}% de opciones de ganar, en lugar del {other}%: vale {value} puntos.",
                        "Una parada de {value} puntos para el {team}: {before}% de opciones de ganar en vez del {other}%.",
                        "El {team} se aferra a su {before}% de opciones de ganar; un gol lo habría dejado en el {other}%."]},
    },
    "red_card": {  # every headline names the player; "kinds" by the card_kind fact, the generic lines when the feed does not say
        "en": {"headlines": ["{player} sent off", "Red card for {player}", "{player} is shown red", "{player} has to go"],
               "kinds": {"second_yellow": {"headlines": ["Second yellow for {player}", "{player} off after a second yellow", "{player}: second yellow, {team} down to ten",
                                                         "Two bookings, and {player} is off"],
                                           "whys": ["Second yellow for {player}: {team} are down to ten and go from {before}% to {after}% to win.",
                                                    "{team} down to ten after a second yellow: from {before}% to {after}% to win.",
                                                    "Booked twice, {player} leaves {team} with ten, from {before}% to {after}% to win."]},
                         "straight_red": {"headlines": ["Straight red for {player}", "{player} sees a straight red", "A straight red, and {player} is off"],
                                          "whys": ["Straight red for {player}: {team} are down to ten and go from {before}% to {after}% to win.",
                                                   "A straight red leaves {team} with ten: from {before}% to {after}% to win."]}},
               "whys": ["Down to ten men, {team} go from {before}% to {after}% to win.", "{team} drop from {before}% to {after}% to win with ten men.",
                        "With a man less, {team} are down to {after}% to win, from {before}%.", "The red card costs {team}: from {before}% to {after}% to win."]},
        "fr": {"headlines": ["{player} expulsé", "Carton rouge pour {player}", "{player} doit sortir", "Rouge pour {player}"],
               "kinds": {"second_yellow": {"headlines": ["Second jaune pour {player}", "{player} exclu sur un second jaune", "Deuxième avertissement pour {player}",
                                                         "{player} averti deux fois et exclu"],
                                           "whys": ["Second jaune pour {player} : {team}, réduit à dix, passe de {before} % à {after} % de chances de gagner.",
                                                    "{team} réduit à dix après un second jaune : de {before} % à {after} % de chances de gagner.",
                                                    "Averti deux fois, {player} laisse {team} à dix, qui passe de {before} % à {after} % de chances de gagner."]},
                         "straight_red": {"headlines": ["Rouge direct pour {player}", "{player} exclu sur un rouge direct", "Carton rouge direct pour {player}"],
                                          "whys": ["Rouge direct pour {player} : {team}, réduit à dix, passe de {before} % à {after} % de chances de gagner.",
                                                   "Un rouge direct laisse {team} à dix : de {before} % à {after} % de chances de gagner."]}},
               "whys": ["Réduit à dix, {team} passe de {before} % à {after} % de chances de gagner.", "{team} tombe de {before} % à {after} % de chances de gagner à dix contre onze.",
                        "Avec un homme de moins, {team} n'a plus que {after} % de chances de gagner, contre {before} %.", "Le carton rouge coûte cher à {team} : de {before} % à {after} % de chances de gagner."]},
        "es": {"headlines": ["{player}, expulsado", "Roja para {player}", "{player} se va a la calle", "Tarjeta roja para {player}"],
               "kinds": {"second_yellow": {"headlines": ["Segunda amarilla para {player}", "{player}, expulsado por doble amarilla", "Doble amarilla para {player}"],
                                           "whys": ["Segunda amarilla para {player}: el {team}, con diez, pasa del {before}% al {after}% de opciones de ganar.",
                                                    "El {team}, con diez tras una segunda amarilla: del {before}% al {after}% de opciones de ganar.",
                                                    "Con dos amarillas, {player} deja al {team} con diez, que pasa del {before}% al {after}% de opciones de ganar."]},
                         "straight_red": {"headlines": ["Roja directa para {player}", "{player}, expulsado con roja directa"],
                                          "whys": ["Roja directa para {player}: el {team}, con diez, pasa del {before}% al {after}% de opciones de ganar.",
                                                   "Una roja directa deja al {team} con diez: del {before}% al {after}% de opciones de ganar."]}},
               "whys": ["Con diez, el {team} pasa del {before}% al {after}% de opciones de ganar.", "El {team} cae del {before}% al {after}% de opciones de ganar con uno menos.",
                        "Con un hombre menos, al {team} le queda un {after}% de opciones de ganar, frente al {before}%.", "La roja le cuesta caro al {team}: del {before}% al {after}% de opciones de ganar."]},
    },
    "chance": {
        "en": {"headlines": ["Big chance for {team}", "{player} so close for {team}", "{player} misses a big one", "{team} go close", "{player} cannot finish"],
               "whys": ["A goal would have taken {team} from {before}% to {other}% to win.", "{team} stay on {before}% to win; that one would have put them on {other}%.",
                        "Had it gone in, {team} would be on {other}% to win instead of {before}%.", "{team} were one finish away from {other}% to win; they stay on {before}%."]},
        "fr": {"headlines": ["Grosse occasion pour {team}", "{player} tout près", "{player} n'en profite pas", "{team} passe tout près", "Occasion manquée pour {team}"],
               "whys": ["Un but aurait fait passer {team} de {before} % à {other} % de chances de gagner.", "{team} reste à {before} % de chances de gagner ; ce but l'aurait porté à {other} %.",
                        "S'il était entré, {team} serait à {other} % de chances de gagner au lieu de {before} %.", "{team} est passé à un geste de {other} % de chances de gagner ; il reste à {before} %."]},
        "es": {"headlines": ["Ocasión clara del {team}", "{player} rozó el gol", "{player} no acierta", "El {team} perdona", "Oportunidad perdida del {team}"],
               "whys": ["Un gol habría llevado al {team} del {before}% al {other}% de opciones de ganar.", "El {team} sigue en el {before}% de opciones de ganar; ese gol lo habría puesto en el {other}%.",
                        "Si entraba, el {team} estaría en el {other}% de opciones de ganar en lugar del {before}%.", "El {team} se quedó a un remate del {other}% de opciones de ganar; sigue en el {before}%."]},
    },
}

LOW_STAKE_TEMPLATES = {  # a goal with no stake: a milestone, never a probability
    "en": {"headlines": ["{scorer} makes it {team_count} for {team}", "A {scorer_nth} for {scorer}", "{scorer} adds the {team_nth}", "{scorer} joins the party for {team}"],
           "whys": ["{team} were already home; this one is for the story of the day.", "{team} had the result in hand; {scorer} adds a line to the story.",
                    "The result was settled; {team} simply add to a memorable day.", "Nothing left to decide, but {team} keep going: a {team_nth} goal."]},
    "fr": {"headlines": ["{scorer} porte {team} à {team_count}", "Un {scorer_nth} pour {scorer}", "{scorer} inscrit le {team_nth}", "{scorer} se joint à la fête"],
           "whys": ["{team} tenait déjà le résultat ; ce but-là est pour l'histoire de la soirée.", "{team} tenait le résultat ; {scorer} continue d'écrire l'histoire.",
                    "Le résultat était acquis ; {team} prolonge simplement une soirée mémorable.", "Plus rien à décider, mais {team} continue : un {team_nth} but."]},
    "es": {"headlines": ["{scorer} hace el {team_nth} del {team}", "El {scorer_nth} de {scorer}", "{scorer} suma otro para el {team}", "{scorer} se suma a la fiesta"],
           "whys": ["El {team} ya tenía el resultado; este es para la historia del día.", "El {team} tenía el partido en el bolsillo; {scorer} sigue escribiendo la historia.",
                    "El resultado estaba decidido; el {team} solo hace más grande un día para recordar.", "Ya no hay nada que decidir, pero el {team} sigue: un {team_nth_adj} gol."]},
}

INSIGHT_TEMPLATES = {  # the fallback of an insight: no number (the history facts are free-form), a line true for its kind
    "en": {"record": [("{subject} sets a record", "{subject} goes where nobody had gone in this league's history."), ("A record for the books", "This match has just set a record in the league's history.")],
           "rarity": [("A rare feat for {subject}", "{subject} does something this league has almost never seen."), ("Something almost never seen", "This league has almost never seen what this match has just produced.")],
           "streak": [("{subject}'s run goes on", "Another match, and the run of {subject} carries on."), ("The run goes on", "The streak carries on, one more match in the league's history.")],
           "milestone": [("A milestone for {subject}", "{subject} reaches a landmark in the league's history."), ("A milestone reached", "A landmark in the league's history is reached in this match.")],
           "storyline": [("The story of {home_team} v {away_team}", "{home_team} and {away_team} add a new chapter to a long story."), ("The story behind the match", "A long story in the league's history is behind this match.")],
           "tactical": [("A pattern from {subject}", "{subject} keep coming back to the same idea in this match."), ("A pattern worth watching", "The same idea keeps coming back in this match.")]},
    "fr": {"record": [("{subject} établit un record", "{subject} va là où personne n'était allé dans l'histoire du championnat."), ("Un record pour l'histoire", "Ce match vient d'établir un record dans l'histoire du championnat.")],
           "rarity": [("Un exploit rare pour {subject}", "{subject} réussit ce que le championnat n'a presque jamais vu."), ("Du jamais ou presque", "Le championnat n'a presque jamais vu ce que ce match vient de produire.")],
           "streak": [("{subject} continue sa série", "Un match de plus, et la série de {subject} continue."), ("La série continue", "La série continue, un match de plus dans l'histoire du championnat.")],
           "milestone": [("Un cap pour {subject}", "{subject} atteint un cap dans l'histoire du championnat."), ("Un cap franchi", "Un cap de l'histoire du championnat est franchi dans ce match.")],
           "storyline": [("{home_team} contre {away_team}, une histoire", "{home_team} et {away_team} ajoutent un chapitre à une longue histoire."), ("L'histoire derrière le match", "Une longue histoire du championnat se cache derrière ce match.")],
           "tactical": [("Une idée signée {subject}", "{subject} revient sans cesse à la même idée dans ce match."), ("Une idée à surveiller", "La même idée revient sans cesse dans ce match.")]},
    "es": {"record": [("{subject} establece un récord", "{subject} llega adonde nadie había llegado en la historia de la liga."), ("Un récord para la historia", "Este partido acaba de establecer un récord en la historia de la liga.")],
           "rarity": [("Una rareza para {subject}", "{subject} logra algo que la liga casi nunca ha visto."), ("Algo casi nunca visto", "La liga casi nunca ha visto lo que este partido acaba de dar.")],
           "streak": [("{subject} sigue con su racha", "Un partido más, y la racha de {subject} continúa."), ("La racha sigue", "La racha continúa, un partido más en la historia de la liga.")],
           "milestone": [("Una marca para {subject}", "{subject} alcanza una marca en la historia de la liga."), ("Una marca alcanzada", "En este partido se alcanza una marca de la historia de la liga.")],
           "storyline": [("{home_team} contra {away_team}, una historia", "{home_team} y {away_team} suman un capítulo a una larga historia."), ("La historia detrás del partido", "Detrás de este partido hay una larga historia de la liga.")],
           "tactical": [("Una idea de {subject}", "{subject} vuelve una y otra vez a la misma idea en este partido."), ("Una idea a vigilar", "La misma idea vuelve una y otra vez en este partido.")]},
}

TEMPLATES = {m: {lang: fr_typography(v) if lang == "fr" else v for lang, v in by_lang.items()} for m, by_lang in TEMPLATES.items()}
LOW_STAKE_TEMPLATES = {lang: fr_typography(v) if lang == "fr" else v for lang, v in LOW_STAKE_TEMPLATES.items()}
INSIGHT_TEMPLATES = {lang: fr_typography(v) if lang == "fr" else v for lang, v in INSIGHT_TEMPLATES.items()}


def minute_number(label: str) -> int:
    found = re.match(r"\d+", str(label))
    return int(found.group(0)) if found else 0


def digits(score_line: Any) -> str | None:
    """"3-2" from "Rovers 3-2 United"."""
    found = re.search(r"\d+-\d+", str(score_line or ""))
    return found.group(0) if found else None


def conditions(card: dict, facts: dict) -> dict:
    """The conditions of the duo lines (`duo.CONDITIONS`): "" when the line is true for this card, None to skip it."""
    slug = card["team_name"].lower()
    swing, value = facts.get("swing_pts"), facts.get("save_value_pts")
    before = facts.get(f"{slug}_wp_before_pct", facts.get(f"{slug}_wp_after_save_pct"))
    other = facts.get(f"{slug}_wp_if_goal_pct")
    goals, team_goals = facts.get("scorer_match_goals"), facts.get("team_goals_after")
    true = {
        "if_home": card["team"] == "home",
        "if_away": card["team"] == "away",
        "if_new_scorer": goals == 1 and isinstance(team_goals, int) and team_goals >= 2,  # his first goal, not his team's: the previous one was someone else's
        "if_tilt": isinstance(swing, int) and abs(swing) > TILT_PTS,
        "if_small_margin": isinstance(value, int) and value < SMALL_MARGIN_PTS,
        "if_big_margin": isinstance(value, int) and value >= SMALL_MARGIN_PTS,
        "if_big_chance": isinstance(before, int) and isinstance(other, int) and abs(other - before) >= SMALL_MARGIN_PTS,
        "if_deflection": card.get("rewrite_cause") == "deflection",
    }
    return {k: "" if true.get(k) else None for k in CONDITIONS}


def template_values(card: dict, facts: dict, lang: str) -> tuple[dict, dict]:
    """The placeholders of a card's templates, the conditions of its duo lines, and the fact field behind each numeric placeholder."""
    slug = card["team_name"].lower()
    teams = teams_in(facts)
    player = card["player"]["name"]
    before_field = f"{slug}_wp_before_pct" if f"{slug}_wp_before_pct" in facts else f"{slug}_wp_after_save_pct"
    values = {
        "team": card["team_name"], "other_team": next((t for t in teams if t != card["team_name"]), None), "scorer": facts.get("scorer"), "player": player,
        "before": facts.get(before_field), "after": facts.get(f"{slug}_wp_after_pct"), "other": facts.get(f"{slug}_wp_if_goal_pct"),
        "swing": facts.get("swing_pts"), "value": facts.get("save_value_pts"), "xg": facts.get("shot_xg_pct"), "minute": facts.get("minute"),
        "de_scorer": fr_de(facts["scorer"]) if facts.get("scorer") else None, "de_player": fr_de(player), "previous_scorer": facts.get("previous_scorer"),
        "score_now": digits(facts.get("score_after")), "score_then": digits(facts.get("score_before")),
    }
    team_n, scorer_n = count_words(lang, facts.get("team_goals_after")), count_words(lang, facts.get("scorer_match_goals"))
    several = facts.get("scorer_match_goals", 0) >= 2
    values |= {"team_nth": team_n.get("nth"), "team_nth_adj": team_n.get("nth_adj"), "team_count": team_n.get("count"),
               "team_count_cap": team_n.get("count", "").capitalize() or None, "scorer_nth": scorer_n.get("nth") if several else None,
               "scorer_nth_adj": scorer_n.get("nth_adj") if several else None, "scorer_count": scorer_n.get("count")}
    values |= conditions(card, facts)
    fields = {"before": before_field, "after": f"{slug}_wp_after_pct", "other": f"{slug}_wp_if_goal_pct", "swing": "swing_pts", "value": "save_value_pts",
              "xg": "shot_xg_pct", "minute": "minute", "score_now": "score_after", "score_then": "score_before"}
    return values, fields


FRESH = {  # closes a required duo line when the match has already heard every variant: the score and the minute make it new
    "en": {"goal": "{score_now} at {minute}.", "other": "Still {score_then} at {minute}."},
    "fr": {"goal": "{score_now} à la {minute}.", "other": "Toujours {score_then} à la {minute}."},
    "es": {"goal": "{score_now} en el {minute}.", "other": "Sigue el {score_then} en el {minute}."},
}
DEMOTED = 500  # a variant the control room turned down repeatedly: after every other one, before a repeat


class _Picker:
    """Chooses among template variants: the least used in the match (and in the draft so far), ties broken by a stable rotation; the variants the producers turned down (`demoted` skeletons, worker.feedback) come last."""

    def __init__(self, memory: PhraseMemory | None, doc_id: str, lang: str, facts: dict, demoted: frozenset = frozenset()) -> None:
        self.memory = memory or PhraseMemory()
        self.facts, self.demoted = facts, demoted
        self.memory.learn_names(facts)
        self.doc_id, self.lang = doc_id, lang
        self.headlines = self.memory.headlines(lang, exclude=doc_id)
        self.verbs = self.memory.verbs(lang, exclude=doc_id)
        self.openings = self.memory.openings(lang, exclude=doc_id)  # a list: an opening used twice weighs twice
        self.phrases = self.memory.phrases(lang, exclude=doc_id)
        self.said = {self.memory.key(t) for l, k, t in self.memory._items(exclude=doc_id) if l == lang and k != "headline"}
        self.tics = self.memory.tics_used(exclude=doc_id)

    def headline(self, pools: list[list[str]], values: dict, fields: dict, slot: str) -> tuple[str, list[dict]] | None:
        best = None
        for rank, pool in enumerate(pools):
            for template in rotated(pool, self.doc_id, self.lang, slot, rank):
                filled = fill(template, values, fields)
                if not filled:
                    continue
                text = filled[0]
                score = (1000 if self.memory.key(text) in self.headlines else 0) + 10 * sum(self.verbs.count(v) for v in self.memory.verbs_in(text, self.lang)) + rank + self.penalty(text)
                if best is None or score < best[0]:
                    best = (score, filled)
        if best is None:
            return None
        if best[0] >= 1000:  # every variant already said in this match: add the score line, which is new
            sa = re.search(r"\d+-\d+", str(values.get("score_after") or ""))
            if sa:
                text = f"{best[1][0]}, {sa.group(0)}"
                return text, best[1][1] + [{"value": int(n), "source_field": "score_after"} for n in sa.group(0).split("-")]
        self.headlines.add(self.memory.key(best[1][0]))
        return best[1]

    def line(self, pool: list[str], values: dict, fields: dict, slot: str, optional: bool = False, fresh: tuple | list = ()) -> tuple[str, list[dict]] | None:
        """The least used variant. When the match has already heard every variant word for word, an optional line is dropped and a required one is closed with a fresh fact (`fresh`, see FRESH)."""
        best = None
        for template in rotated(pool, self.doc_id, self.lang, slot):
            filled = fill(template, values, fields)
            if not filled:
                continue
            repeat = self.memory.key(filled[0]) in self.said
            score = 1000 * repeat + 10 * self.openings.count(self.memory.opening(filled[0])) + len(self.memory.ngrams(filled[0]) & self.phrases) + self.penalty(filled[0])
            if best is None or score < best[0]:
                best = (score, filled)
        if best is not None and best[0] >= 1000 and not optional:
            for suffix in fresh:
                extra = fill(suffix, values, fields)
                text = f"{best[1][0]} {extra[0]}" if extra else None
                if text and self.memory.key(text) not in self.said:
                    best = (best[0] - 1000, (text, best[1][1] + extra[1]))
                    break
        if best is None or (optional and best[0] >= 1000):
            return None
        return self._said(best[1])

    def first(self, pool: list[str], values: dict, fields: dict) -> tuple[str, list[dict]] | None:
        """The first variant, in the order given, that the match has not heard word for word (the plain phrasing first, the others for a repeat); None when every one was said."""
        for template in pool:
            filled = fill(template, values, fields)
            if filled and self.memory.key(filled[0]) not in self.said:
                return self._said(filled)
        return None

    def _said(self, filled: tuple[str, list[dict]]) -> tuple[str, list[dict]]:
        self.openings.append(self.memory.opening(filled[0]))
        self.phrases |= self.memory.ngrams(filled[0])
        self.said.add(self.memory.key(filled[0]))
        return filled

    def penalty(self, text: str) -> int:
        return DEMOTED if self.demoted and skeleton(text, self.facts) in self.demoted else 0

    def tic(self, speaker: str) -> str | None:
        for t in rotated(list(PERSONAS[speaker].voices[self.lang].tics), self.doc_id, self.lang, speaker, "tic"):
            if t not in self.tics:
                self.tics.add(t)
                return t
        return None


def story_key(facts: dict) -> str:
    n = facts.get("scorer_match_goals")
    if isinstance(n, int) and n >= 2:
        return "brace" if n == 2 else "hat_trick" if n == 3 else "four_plus"
    return "late" if minute_number(facts.get("minute", "")) >= 80 else "any"


class TemplateWriter:
    """Fixed sentences filled from the facts: no model, always verified."""

    name = "template"

    def __init__(self, demoted=None) -> None:
        self.demoted = frozenset(demoted or ())

    def write(self, card: dict, facts: dict, languages: list[str], memory: PhraseMemory | None = None, duo: bool = False) -> str:
        """The card texts (a JSON array), or with `duo` an object {"texts", "duo"}."""
        texts, lines = [], []
        for lang in languages:
            values, fields = template_values(card, facts, lang)
            values["score_after"] = facts.get("score_after")
            pick = _Picker(memory, card["id"], lang, facts, self.demoted)
            if facts.get("stake") == "low":
                t = LOW_STAKE_TEMPLATES[lang]
                pools, whys = [t["headlines"]], t["whys"]
            else:
                t = TEMPLATES[card["moment"]][lang]
                kind = t.get("kinds", {}).get(facts.get("card_kind"))
                if kind:  # how the player was sent off is told: the headlines of that kind only, its whys first
                    pools, whys = [kind["headlines"]], kind["whys"] + t["whys"]
                else:
                    pools, whys = [t.get("situations", {}).get(facts.get("goal_situation"), []), t["headlines"]], t["whys"]
            headline, h_claims = pick.headline([p for p in pools if p], values, fields, "headline")
            pick.openings.append(pick.memory.opening(headline))  # neither the why nor the duo starts like the headline
            why, w_claims = pick.line(whys, values, fields, "why")
            claims = [dict(c) for c in {tuple(sorted(c.items())): c for c in h_claims + w_claims}.values()]
            texts.append({"headline": headline, "why": why, "lang": lang, "claims": claims})
            if duo:
                lines += self._duo(card, facts, lang, values, fields, pick)
        return json.dumps({"texts": texts, "duo": lines} if duo else texts, ensure_ascii=False)

    def _duo(self, card: dict, facts: dict, lang: str, values: dict, fields: dict, pick: _Picker) -> list[dict]:
        if facts.get("previous_scorer"):  # the feed credited the goal to another player: the duo tells the correction, not the goal again
            return self._reassigned(lang, values, fields, pick)
        kind = "goal_low" if facts.get("stake") == "low" else card["moment"]
        slots = LINES[kind][lang]
        seed = stable(card["id"], card.get("version", 1), lang, "n")
        big = max(abs(facts.get("swing_pts") or 0), abs(facts.get("save_value_pts") or 0)) >= 30
        n = 2 + seed % 3
        if big:
            n = max(n, 3)
        if kind == "goal_low":
            n = min(n, 3)
        more = slots.get("eye_more", {}).get(story_key(facts) if kind.startswith("goal") else "any", []) or slots.get("eye_more", {}).get("any", [])
        heart = slots["heart"]
        if isinstance(heart, dict):  # a goal by its situation, a red card by how the player was sent off
            heart = heart.get(facts.get("goal_situation") or facts.get("card_kind")) or heart.get("any", [])
        if kind.startswith("goal") and facts.get("score_after") in pick.memory.ruled_out:
            heart = LINES["goal"][lang]["again"]  # VAR took a goal at this score away earlier: this one counts
        fresh = [FRESH[lang]["goal" if kind.startswith("goal") else "other"]]
        plan = [(HEART, heart, "heart"), (EYE, slots["eye"], "eye"), (HEART, slots["heart_back"], "heart_back"), (EYE, more, "eye_more")]
        lines = []
        if kind == "save" and seed % 2 == 0 and n >= 3:  # the old goalkeeper speaks first, if the match has not heard it yet
            opener = pick.line(slots["eye_open"], values, fields, "eye_open", optional=True)
            if opener:
                lines.append({"speaker": EYE, "lang": lang, "line": opener[0], "claims": opener[1]})
                plan = plan[:3]
        if kind.startswith("goal") and isinstance(facts.get("scorer_match_goals"), int) and facts["scorer_match_goals"] >= 3 and more:
            n = max(n, 4)  # the story of the scorer is worth a line
        for speaker, pool, slot in plan:
            if len(lines) >= n:
                break
            if not pool:
                break
            filled = pick.line(pool, values, fields, slot, optional=slot in ("heart_back", "eye_more"), fresh=fresh)
            if not filled:
                break
            text, claims = filled
            if slot == "heart" and big:
                tic = pick.tic(HEART)
                if tic:
                    text = f"{tic} {text}"
            lines.append({"speaker": speaker, "lang": lang, "line": text, "claims": claims})
        season = season_lines(card, facts, lang, pick) if kind == "goal" and len(lines) >= 2 else None
        if season:  # the table matters more than a third remark: the duo ends on it
            lines = lines[:2] + season
        return lines

    @staticmethod
    def _reassigned(lang: str, values: dict, fields: dict, pick: _Picker) -> list[dict]:
        slots, fresh = LINES["reassigned"][lang], [FRESH[lang]["goal"]]
        out = []
        for speaker, slot in ((HEART, "heart"), (EYE, "eye")):
            filled = pick.line(slots[slot], values, fields, slot, fresh=fresh)
            if filled:
                out.append({"speaker": speaker, "lang": lang, "line": filled[0], "claims": filled[1]})
        return out

    def ruling(self, card: dict, facts: dict, languages: list[str], memory: PhraseMemory | None = None) -> list[dict]:
        """What the duo says when VAR rules a goal out."""
        out = []
        for lang in languages:
            values, fields = template_values(card, facts, lang)
            slots = LINES["ruling"][lang]
            pick = _Picker(memory, card["id"] + "#ruling", lang, facts, self.demoted)
            reason = facts.get("ruled_out_reason", "other")
            key = f"offside_{facts['offside_call']}" if reason == "offside" and facts.get("offside_call") else reason if reason in slots["eye"] else "other"
            fresh = [FRESH[lang]["other"]]  # the score the goal would have made does not stand
            heart = pick.line(slots["heart"], values, fields, "heart", fresh=fresh)
            eye = pick.line(slots["eye"][key], values, fields, "eye", fresh=fresh)
            if key == "offside_tight" and eye:
                tic = PERSONAS[EYE].voices[lang].tics[0]  # "Look at the line."
                if tic not in pick.tics:
                    eye = (f"{tic} {eye[0]}", eye[1])
            back = pick.line(slots["heart_back"].get(key) or slots["heart_back"]["any"], values, fields, "heart_back", optional=True)
            out += [{"speaker": s, "lang": lang, "line": f[0], "claims": f[1]} for s, f in ((HEART, heart), (EYE, eye), (HEART, back)) if f]
        return out

    def write_insight(self, doc: dict, facts: dict, languages: list[str], memory: PhraseMemory | None = None, duo: bool = False) -> str:
        """An insight or the storyline: the document's own texts when it brings verified ones, else its family's texts, else a line true for its kind; the duo lines of its family, else of its kind."""
        kind = doc.get("kind") or ("storyline" if doc.get("type") == "storyline" else "record")
        subject = next((str(facts[k]) for k in ("player", "player_name", "scorer", "subject", "team", "club", "team_name") if isinstance(facts.get(k), str)), None)
        own = {t.get("lang"): t for t in doc.get("texts") or [] if isinstance(t, dict)}
        air, _ = on_air(facts, {})  # a text of the document's own is kept only if it says nothing off air
        texts, lines = [], []
        for lang in languages:
            pick = _Picker(memory, doc["id"], lang, facts, self.demoted)
            values = {"subject": subject, "home_team": facts.get("home_team"), "away_team": facts.get("away_team")}
            mine = own.get(lang)
            if mine and verify(json.dumps([mine], ensure_ascii=False), [lang], air)[1]["verified"] and memory_key_free(pick, mine):
                texts.append(mine)  # the document's text, the family's duo lines
                fact = insight_fact_text(doc, facts, lang, pick, duo) if duo else None
            else:
                fact = insight_fact_text(doc, facts, lang, pick, duo)
                texts.append(fact[0] if fact else self._kind_text(doc, kind, lang, values, pick))
            if duo:
                lines += (fact[1] if fact else None) or self._kind_lines(kind, lang, values, pick)
        return json.dumps({"texts": texts, "duo": lines} if duo else texts, ensure_ascii=False)

    @staticmethod
    def _kind_text(doc: dict, kind: str, lang: str, values: dict, pick: _Picker) -> dict:
        """A headline and why true for the kind of fact, with no number."""
        pairs = INSIGHT_TEMPLATES[lang].get(kind) or INSIGHT_TEMPLATES[lang]["storyline"]
        chosen = None
        for headline, why in rotated(pairs, doc["id"], lang):
            h, w = fill(headline, values, {}), fill(why, values, {})
            if h and w and pick.memory.key(h[0]) not in pick.headlines:
                chosen = (h[0], w[0])
                break
        if chosen is None:
            chosen = (fill(pairs[-1][0], values, {}) or (pairs[-1][0], []))[0], (fill(pairs[-1][1], values, {}) or (pairs[-1][1], []))[0]
        return {"headline": chosen[0], "why": chosen[1], "lang": lang, "claims": []}

    @staticmethod
    def _kind_lines(kind: str, lang: str, values: dict, pick: _Picker) -> list[dict]:
        """Two duo lines true for the kind of fact, with no number; the storyline's lines when the kind's were all said."""
        slots = LINES["insight"][lang]
        out = []
        for speaker, slot in ((HEART, "heart"), (EYE, "eye")):
            filled = (pick.line(slots[slot].get(kind, []), values, {}, slot, optional=True) or pick.line(slots[slot]["storyline"], values, {}, slot, optional=True)
                      or pick.line(slots[slot]["storyline"], values, {}, slot))
            out.append({"speaker": speaker, "lang": lang, "line": filled[0], "claims": filled[1]})
        return out


def insight_fact_text(doc: dict, facts: dict, lang: str, pick: _Picker, duo: bool) -> tuple[dict, list[dict] | None] | None:
    """The text and duo lines of an insight from its family's templates, with its numbers claimed: None when no family covers it or its text cannot be filled; no lines (None) when the match has already heard every line the family has for it."""
    from worker import insight_texts as it

    fam = it.family(doc)
    if fam is None:
        return None
    t = it.T[fam][lang]
    vals, fields = it.values(facts, lang)
    stat = "top_fact" in facts  # the stat of the match

    def pool(templates: list[str]) -> list[str]:
        return [it.clean(x) for x in templates if it.usable(x, facts)]

    heads = pool(it.part(t, "headlines", facts))
    if stat:
        heads = [h for h in heads if ":" not in h] or heads  # after its own prefix, a second colon reads badly
    headline = pick.headline([heads], vals, fields, "headline")
    why = pick.line(pool(it.part(t, "whys", facts)), vals, fields, "why")
    if not headline or not why:
        return None
    text, claims = headline[0], headline[1]
    body, body_claims = why
    if stat:
        names = {w for v in facts.values() if isinstance(v, str) for w in v.split()}
        text = it.STAT_PREFIX[lang] + (text if text.split()[0] in names else text[:1].lower() + text[1:])
        extra = fill(it.STAT_WHY[lang], vals, fields)
        if extra:
            body, body_claims = body + extra[0], body_claims + extra[1]
    out = {"headline": text, "why": body, "lang": lang, "claims": claims + body_claims}
    if not duo:
        return out, []
    hearts = it.STAT_HEART.get(fam, it.STAT_HEART["any"])[lang] if stat else it.part(t, "heart", facts)
    heart = pick.line(pool(hearts), vals, fields, "heart", optional=True)
    eye = pick.line(pool(it.part(t, "eye", facts)), vals, fields, "eye", optional=True)
    if not heart or not eye:
        return out, None
    return out, [{"speaker": HEART, "lang": lang, "line": heart[0], "claims": heart[1]}, {"speaker": EYE, "lang": lang, "line": eye[0], "claims": eye[1]}]


SEASON_LINES = {  # the duo on what a goal does to the table: Wren asks, Joss answers with the rank and the relegation chance if the score holds
    "en": {"ask": ["And the table, Joss?", "What does it do to the table, Joss?", "How does the table look now, Joss?", "Joss, what about the table?",
                   "And in the standings, Joss?", "What does {score} do to the table, Joss?"],
           "leaves": ["If it stays {score}, {club} climb out of the drop zone, up to {rank_ord}.", "If it stays {score}, {club} are out of the drop zone and up to {rank_ord}."],
           "enters": ["If it stays {score}, {club} drop into the drop zone, down to {rank_ord}.", "If it stays {score}, {club} are in the drop zone, down in {rank_ord}."],
           "up": ["If it stays {score}, {club} go up to {rank_ord}, and their chance of going down falls to {chance}%.",
                  "If it stays {score}, {club} rise to {rank_ord}, with a {chance}% chance of going down."],
           "down": ["If it stays {score}, {club} slip to {rank_ord}, and their chance of going down rises to {chance}%.",
                    "If it stays {score}, {club} fall to {rank_ord}, with a {chance}% chance of going down."]},
    "fr": {"ask": ["Et au classement, Joss ?", "Qu'est-ce que ça change au classement, Joss ?", "Et le classement, Joss, il dit quoi ?", "Joss, et le classement ?",
                   "Où en est-on au classement, Joss ?", "Que change ce {score} au classement, Joss ?"],
           "leaves": ["Si ça reste {score}, {club} sort de la zone rouge et remonte {rank_ord}.", "Si ça reste {score}, {club} quitte la zone rouge, {rank_ord}."],
           "enters": ["Si ça reste {score}, {club} tombe dans la zone rouge, {rank_ord}.", "Si ça reste {score}, {club} plonge dans la zone rouge, {rank_ord}."],
           "up": ["Si ça reste {score}, {club} remonte {rank_ord}, et son risque de descente tombe à {chance} %.",
                  "Si ça reste {score}, {club} grimpe {rank_ord}, avec {chance} % de risque de descente."],
           "down": ["Si ça reste {score}, {club} recule {rank_ord}, et son risque de descente monte à {chance} %.",
                    "Si ça reste {score}, {club} glisse {rank_ord}, avec {chance} % de risque de descente."]},
    "es": {"ask": ["¿Y en la tabla, Joss?", "¿Qué cambia en la tabla, Joss?", "¿Cómo queda la tabla, Joss?", "Joss, ¿y la clasificación?",
                   "¿Y la clasificación, Joss?", "¿Qué hace este {score} en la tabla, Joss?"],
           "leaves": ["Si se mantiene el {score}, el {club} sale del descenso y sube al {rank_ord} puesto.", "Si se mantiene el {score}, el {club} abandona el descenso: {rank_ord}."],
           "enters": ["Si se mantiene el {score}, el {club} cae al descenso, {rank_ord}.", "Si se mantiene el {score}, el {club} se mete en el descenso: {rank_ord}."],
           "up": ["Si se mantiene el {score}, el {club} sube al {rank_ord} puesto, y su riesgo de descenso baja al {chance}%.",
                  "Si se mantiene el {score}, el {club} escala al {rank_ord} puesto, con un {chance}% de riesgo de descenso."],
           "down": ["Si se mantiene el {score}, el {club} baja al {rank_ord} puesto, y su riesgo de descenso sube al {chance}%.",
                    "Si se mantiene el {score}, el {club} cae al {rank_ord} puesto, con un {chance}% de riesgo de descenso."]},
}
SEASON_LINES = {lang: fr_typography(v) if lang == "fr" else v for lang, v in SEASON_LINES.items()}


def season_lines(card: dict, facts: dict, lang: str, pick: _Picker) -> list[dict] | None:
    """Two duo lines on the table when a goal changes it: a club entering or leaving the drop zone first, else the scoring club moving two places or more. None when the match has already heard both lines word for word."""
    from worker.insight_texts import Ord

    score_field = "season_score_now"
    if not isinstance(facts.get(score_field), str):
        return None
    scoring = card["team_name"]
    clubs = sorted({k[len("season_"):-len("_rank_if_score_holds")] for k in facts if k.startswith("season_") and k.endswith("_rank_if_score_holds")})
    names = {c: next((n for n in (facts.get("score_before", "") or "").replace("-", " ").split() if n.lower() == c), None) for c in clubs}
    pickd = None
    for c in clubs:
        for move in ("leaves", "enters"):
            if facts.get(f"season_{c}_{move}_drop_zone_if_score_holds"):
                pickd = pickd or (c, move)
    slug = scoring.lower().replace(" ", "_")
    if pickd is None and f"season_{slug}_rank_if_score_holds" in facts:
        before, after = facts[f"season_{slug}_rank_at_kickoff"], facts[f"season_{slug}_rank_if_score_holds"]
        if abs(after - before) >= 2 and f"season_{slug}_relegation_chance_if_score_holds_pct" in facts:
            pickd = (slug, "up" if after < before else "down")
    if pickd is None:
        return None
    c, move = pickd
    club = scoring if c == slug else (names.get(c) or c.replace("_", " ").title())
    score = re.search(r"\d+-\d+", facts[score_field]).group(0)
    rank = facts[f"season_{c}_rank_if_score_holds"]
    values = {"score": score, "club": club, "rank_ord": Ord(rank, lang), "chance": facts.get(f"season_{c}_relegation_chance_if_score_holds_pct")}
    fields = {"score": score_field, "rank_ord": f"season_{c}_rank_if_score_holds", "chance": f"season_{c}_relegation_chance_if_score_holds_pct"}
    answer = pick.first(SEASON_LINES[lang][move], values, fields)  # the plain answer, the second one when a goal VAR ruled out already gave it
    ask = pick.line(SEASON_LINES[lang]["ask"], values, fields, "season_ask", optional=True) if answer else None
    if not answer or not ask:
        return None
    return [{"speaker": HEART, "lang": lang, "line": ask[0], "claims": ask[1]}, {"speaker": EYE, "lang": lang, "line": answer[0], "claims": answer[1]}]


def memory_key_free(pick: _Picker, text: dict) -> bool:
    return pick.memory.key(str(text.get("headline", ""))) not in pick.headlines


class FoundryWriter:
    """The Writer agent on Microsoft Foundry (Agent Framework, Responses API), with get_moment as a function tool."""

    def __init__(self, model: str | None = None, effort: str | None = None, max_tokens: int = 3000) -> None:
        from agent_framework import Agent, tool
        from agent_framework.foundry import FoundryChatClient
        from azure.identity import AzureCliCredential, ChainedTokenCredential, ManagedIdentityCredential

        self.model = model or os.environ["FOUNDRY_MODEL"]
        self.name = self.model
        self.facts: dict[str, tuple[dict, dict]] = {}
        store = self.facts

        @tool(approval_mode="never_require")
        def get_moment(card_id: str) -> str:
            """Return the facts of a card and the meaning of each field, as JSON."""
            if card_id not in store:
                return json.dumps({"error": f"unknown card_id {card_id}"})
            facts, meanings = store[card_id]
            return json.dumps({"facts": facts, "field_meanings": meanings}, ensure_ascii=False)

        credential = ChainedTokenCredential(ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID")), AzureCliCredential())  # managed identity in Azure, az login locally
        client = FoundryChatClient(project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], model=self.model, credential=credential)
        options: dict[str, Any] = {"max_tokens": max_tokens}  # GPT-5 models take no temperature
        effort = effort or os.environ.get("FOUNDRY_REASONING_EFFORT")
        if effort:
            options["reasoning"] = {"effort": effort}
        self.agent = Agent(client=client, name="writer", instructions=WRITER_INSTRUCTIONS, tools=[get_moment], default_options=options)

    async def write_async(self, card: dict, facts: tuple[dict, dict], languages: list[str], note: str | None = None, avoid: dict | None = None, duo: bool = False,
                          preferences: str | None = None) -> str:
        self.facts[card["id"]] = facts
        response = await self.agent.run(writer_prompt(card, languages, note, avoid, duo, preferences))
        return response.text


def writer_prompt(card: dict, languages: list[str], note: str | None = None, avoid: dict | None = None, duo: bool = False, preferences: str | None = None) -> str:
    """The user message of one Writer call: the request, the control room's preferences (`worker.feedback.prompt_block`, bounded), and the Verifier's note on a retry."""
    prompt = f"Write the cards for this request: {json.dumps(writer_request(card, languages, avoid, duo), ensure_ascii=False)}"
    if preferences:
        prompt += f"\n{preferences}"
    if note:
        prompt += f"\nProducer note: {note}"
    return prompt


def writer_request(card: dict, languages: list[str], avoid: dict | None = None, duo: bool = False) -> dict:
    """The request the Writer agent receives (the facts come through get_moment)."""
    request: dict[str, Any] = {"card_id": card["id"], "languages": languages}
    if card.get("type") in ("insight", "storyline"):
        request["document"] = card["type"]
    if duo:
        request["duo"] = True
    if avoid:
        request["avoid"] = avoid
    return request


@dataclass
class Draft:
    card_id: str
    version: int
    writer: str
    attempts: int
    texts: list[dict]
    report: dict
    machine_translated: list[str] = field(default_factory=list)
    duo: list[dict] = field(default_factory=list)
    duo_writer: str | None = None


def accepts(writer, name: str) -> bool:
    """Whether writer.write_async takes a keyword argument (older writers and test fakes do not take avoid or duo)."""
    try:
        params = inspect.signature(writer.write_async).parameters
    except (TypeError, ValueError, AttributeError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def _draft(doc: dict, facts: dict, meanings: dict, writer, languages: list[str], calls_left: int, memory: PhraseMemory | None, duo: bool, template,
                 preferences: str | None = None) -> Draft:
    """The model, one retry told what the Verifier rejected, then the template. Model texts that passed are kept when only the duo failed."""
    attempts, note, kept, done = 0, None, None, None
    ask_duo = duo and accepts(writer, "duo")
    if memory is not None:
        memory.learn_names(facts)
    if not isinstance(writer, TemplateWriter):
        for _ in range(min(2, calls_left)):
            attempts += 1
            kwargs: dict[str, Any] = {}
            if note:
                kwargs["note"] = note
            if ask_duo:
                kwargs["duo"] = True
            if memory is not None and accepts(writer, "avoid"):
                kwargs["avoid"] = memory.avoid(languages)
            if preferences and accepts(writer, "preferences"):
                kwargs["preferences"] = preferences
            try:
                text = await writer.write_async(doc, (facts, meanings), languages, **kwargs)
            except Exception as error:  # a model or network failure falls through to the template
                text = json.dumps({"error": f"{type(error).__name__}: {error}"[:300]})
            texts, lines, report = verify_reply(text, languages, facts, duo=ask_duo, memory=memory, doc_id=doc["id"])
            if report["verified"]:
                done = (writer.name, texts, lines if ask_duo else None)
                break
            if report["texts_verified"]:
                kept = (writer.name, texts)
            note = feedback(report)
    name, texts, lines = done or ((kept[0], kept[1], None) if kept else ("template", None, None))
    duo_name = name if lines is not None else ("template" if duo else None)
    if texts is None or (duo and lines is None):
        fallback_texts, fallback_lines, _ = verify_reply(template(memory), languages, facts, duo=duo)
        texts = texts if texts is not None else fallback_texts
        lines = lines if lines is not None else fallback_lines
    _, _, report = verify_reply(json.dumps({"texts": texts, "duo": lines or []}, ensure_ascii=False), languages, facts, duo=duo, memory=memory, doc_id=doc["id"])
    if memory is not None:
        memory.record(doc["id"], texts, lines or [], facts, version=doc.get("version", 1))
    return Draft(doc["id"], doc.get("version", 1), name, attempts, texts, report, sorted(MACHINE_TRANSLATED & set(languages)), lines or [], duo_name)


async def draft(card: dict, teams: dict[str, str], writer, languages: list[str] = list(LANGUAGES), calls_left: int = 2, memory: PhraseMemory | None = None, duo: bool = False,
                preferences: str | None = None, demoted=None) -> Draft:
    """Writes and verifies a card: the model, one retry told what the Verifier rejected, then the template. `calls_left` caps the model calls this card may still make; `memory` is what the match already said; `duo` adds the commentary lines; `preferences` (the control room's, in the prompt) and `demoted` (template variants picked last) come from the producers' feedback."""
    facts, meanings = card_facts(card, teams)
    return await _draft(card, facts, meanings, writer, languages, calls_left, memory, duo,
                        lambda mem: TemplateWriter(demoted).write(card, facts, languages, memory=mem, duo=duo), preferences)


async def draft_insight(doc: dict, teams: dict[str, str], writer, languages: list[str] = list(LANGUAGES), calls_left: int = 2, memory: PhraseMemory | None = None, duo: bool = False,
                        preferences: str | None = None, demoted=None) -> Draft:
    """Writes and verifies an insight or the storyline, through the same path as a card. The model and the Verifier get the on-air facts only (`on_air`); the templates read every fact to know which lines are true (a feat seen at least once a season is told without the rate)."""
    facts, meanings = insight_facts(doc, teams)
    air, air_meanings = on_air(facts, meanings)
    return await _draft(doc, air, air_meanings, writer, languages, calls_left, memory, duo,
                        lambda mem: TemplateWriter(demoted).write_insight(doc, facts, languages, memory=mem, duo=duo), preferences)


def ruling_duo(card: dict, teams: dict[str, str], languages: list[str] = list(LANGUAGES), memory: PhraseMemory | None = None) -> tuple[list[dict], dict]:
    """The duo's reaction when VAR rules a goal out (templates only: no model call for a retraction), checked like any duo. The memory keeps the score the goal would have made: the next goal at that score is the one that counts."""
    facts, _ = ruling_facts(card, teams)
    lines = TemplateWriter().ruling(card, facts, languages, memory)
    report = verify_duo(lines, languages, facts)
    if memory is not None:
        memory.mark_ruled_out(facts.get("score_after"))
        if report["verified"]:
            memory.record(card["id"] + "#ruling", [], lines, facts)
    return (lines if report["verified"] else []), report
