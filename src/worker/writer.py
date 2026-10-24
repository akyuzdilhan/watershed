"""The Writer and the Verifier: from a Spotter card to on-air text in several languages, every number checked.

The code computes every number and sends it under an explicit field name (`card_facts`); the model only writes, and lists in `claims` each number it used with the field it comes from. The Verifier checks those claims, rejects any number nobody claimed, a percentage taken from a field that is not one, and flags any name that is not in the facts. It also applies broadcast style rules per language (`LANGUAGE_RULES`, `STRUCTURAL_RULES`): an error fails the draft, a warning is only reported. A failed draft gets one retry, told what was rejected, then a fixed template takes over, so a card is never blocked by the model.

The model runs on Microsoft Foundry through Agent Framework (`FoundryWriter`, a function tool `get_moment`); `TemplateWriter` needs no Azure and serves the tests and the fallback.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

LANGUAGES = ("en", "fr", "es")
MACHINE_TRANSLATED = {"es"}  # shown on air with a "machine-translated" label

WRITER_INSTRUCTIONS = """You write live football broadcast cards: a headline and one sentence, "why", shown on screen during the match like a professional TV graphic.

Facts: call the get_moment tool with the card_id to get the facts and the meaning of each field. Use ONLY those facts. Never invent a statistic, name or fact. If something you are asked for is not in the facts, write "not available" for it.

Numbers. Each number has a field name that says what it means: never use a number with another meaning. For example, a win probability "if goal" is a chance of winning, never a chance of scoring. Do not compute any new number. Write every number exactly as given: whole numbers, no sign (never +24), no decimals.
- Fields ending in _pct are percentages: write them with a % sign (34%, never 0.34).
- Fields ending in _pts are percentage points: always write the word points (French: points, Spanish: puntos) right after the number, never a % sign.

Content. Explain why the moment matters through the change in win probability, and credit the action that caused it (a save, not the shot it stopped). Every sentence that gives a win probability names the team it belongs to: rovers_wp_after_pct is the chance of Rovers. Never write "win chance", "the chances" or "les chances" without the team.
- Headline: at most 8 words, names the player or the team. No minute and no number at the start: the graphic already shows the minute and the score.
- Why: one sentence, at most 25 words.
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


def shown(pct: int) -> int:
    """A win probability on air: never 0% or 100% before the final whistle."""
    return min(max(pct, 1), 99)


def card_facts(card: dict, teams: dict[str, str]) -> tuple[dict, dict]:
    """Facts for the Writer, with explicit names, and what each field means."""
    team, name = card["team"], card["team_name"]
    other = teams["away" if team == "home" else "home"]
    slug = name.lower()
    sb = card["score_before"]
    score = f"{teams['home']} {sb['home']}-{sb['away']} {teams['away']}"
    f = card["facts"]
    facts: dict[str, Any] = {"minute": card["minute_label"], "score_before": score}
    meanings = {"minute": "match minute of the moment", "score_before": "score just before the moment"}
    player = card["player"]["name"]
    if card["moment"] == "goal":
        after = dict(sb)
        after[team] += 1
        facts |= {"event": f"{name} goal", "scorer": player, "score_after": f"{teams['home']} {after['home']}-{after['away']} {teams['away']}",
                  f"{slug}_wp_before_pct": shown(f["team_wp_before_pct"]), f"{slug}_wp_after_pct": shown(f["team_wp_after_pct"])}
        facts["swing_pts"] = facts[f"{slug}_wp_after_pct"] - facts[f"{slug}_wp_before_pct"]
        meanings |= {"scorer": "player who scored", "score_after": "score after the goal",
                     f"{slug}_wp_before_pct": f"{name}'s chance of winning the match just before the goal, in %",
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
        meanings |= {"player_sent_off": "player shown the red card", f"{slug}_wp_before_pct": f"{name}'s chance of winning the match before the red card, in %",
                     f"{slug}_wp_after_pct": f"{name}'s chance of winning the match with ten men, in %"}
    else:  # a chance that did not go in
        facts |= {"event": f"{name} chance missed", "shooter": player, "shot_xg_pct": f["xg_pct"],
                  f"{slug}_wp_before_pct": shown(f["team_wp_before_pct"]), f"{slug}_wp_if_goal_pct": shown(f["team_wp_if_goal_pct"])}
        meanings |= {"shooter": "player who took the shot", "shot_xg_pct": "chance the shot had of being a goal (expected goals), in %",
                     f"{slug}_wp_before_pct": f"{name}'s chance of winning the match, in %",
                     f"{slug}_wp_if_goal_pct": f"{name}'s chance of winning the match if the shot had gone in, in %"}
    return facts, meanings


# --- Verifier -------------------------------------------------------------------------------------

def numbers_in(text: str) -> list[tuple[float, bool]]:
    return [(float(m.group(1).replace(",", ".")), bool(m.group(2))) for m in re.finditer(r"(\d+(?:[.,]\d+)?)(\s?%)?", normalise(text))]


def normalise(text: str) -> str:
    return text.replace("\xa0", " ").replace("\u202f", " ")


def claim_ok(claim: Any, facts: dict) -> bool:
    if not isinstance(claim, dict) or not isinstance(claim.get("value"), (int, float)):
        return False
    src = claim.get("source_field")
    value = facts.get(src)
    if isinstance(value, str):  # a score or a minute: the number must appear in it
        return claim["value"] in [float(n) for n in re.findall(r"\d+", value)]
    return isinstance(value, (int, float)) and float(value) == float(claim["value"])


def names_ok(text: str, facts: dict) -> list[str]:
    """Capitalised words that are neither in the facts nor at the start of a sentence: possible invented names."""
    allowed = " ".join(str(v) for v in facts.values())
    words = re.findall(r"(?<![.!?:]\s)(?<!^)\b([A-ZÀ-Ý][\wÀ-ÿ'-]+)", text)
    return sorted({w for w in words if w not in allowed})


# --- style rules ----------------------------------------------------------------------------------
# Every rule is data: a code, a severity and a pattern. An "error" fails the draft (one retry, then the template), a "warning" is
# only reported. In a pattern, {team} stands for each club name of the match. The checks that need the claims (which team a
# probability belongs to, the unit of a number) are listed in STRUCTURAL_RULES and implemented in style_issues.

FR_ARTICLE = r"(?i:\b(?:l['’]\s?|d['’]\s?|(?:le|la|les|du|des|au|aux)\s+))"

LANGUAGE_RULES: dict[str, list[tuple[str, str, str]]] = {
    "en": [
        ("jargon", "warning", r"(?i)\bwin chances?\b"),
    ],
    "fr": [
        ("forbidden_word", "error", r"(?i)\bswing\w*"),
        ("forbidden_word", "error", r"(?i)\bmomentum\b"),
        ("forbidden_word", "error", r"(?i)\bgame[- ]?changer\b"),
        ("article_before_team", "error", FR_ARTICLE + r"{team}\b"),
        ("anglicism", "warning", r"(?i)\b(?:goal|save|clean sheet|turnover|win)\b"),
        ("percent_without_space", "warning", r"\d%"),
    ],
    "es": [
        ("forbidden_word", "error", r"(?i)\bswing\w*"),
        ("forbidden_word", "error", r"(?i)\bmomentum\b"),
        ("forbidden_word", "error", r"(?i)\bgame[- ]?changer\b"),
        ("clumsy_phrase", "warning", r"(?i)\ben puntos de diferencia\b"),
        ("anglicism", "warning", r"(?i)\b(?:goal|save|clean sheet|win)\b"),
    ],
}

# Words that speak of a win probability: a sentence that uses one must name a club.
PROBABILITY_WORDS = {
    "en": r"(?i)\b(?:win(?:ning)? (?:chances?|probability|odds)|chances? of winning|to win)\b",
    "fr": r"(?i)\b(?:chances?|probabilités?)\b",
    "es": r"(?i)\b(?:opciones|probabilidad(?:es)?|posibilidades)\b",
}
POINT_WORDS = {"en": r"points?", "fr": r"points?", "es": r"puntos?"}

STRUCTURAL_RULES = {
    "signed_number": ("error", r"(?<![\w])[+±]\s?\d+"),  # "+24": say the direction with a verb
    "decimal_percent": ("error", r"\d+[.,]\d+\s?%"),  # "34.5%": percentages are whole numbers
    "headline_starts_with_number": ("error", r"^\W*\d"),  # "52' ...": the graphic already shows the minute
    "minute_in_headline": ("warning", r"(?i)\d+(?:\+\d+)?\s?['’′]|\bmin(?:ute|uto)?s?\b"),
    "probability_without_team": ("error", None),  # a probability word in a sentence that names no club
    "probability_number_without_team": ("error", None),  # a number from a <team>_wp_ field in a sentence without that team
    "points_unit_missing": ("error", None),  # a number from a _pts field not followed by the word for points
    "points_not_from_pts_field": ("error", None),  # "54 points" where 54 is a percentage
    "too_long": ("warning", None),  # headline over 8 words or why over 25
}
HEADLINE_MAX_WORDS, WHY_MAX_WORDS = 8, 25


def teams_in(facts: dict) -> list[str]:
    """The two club names, read from the score line "Rovers 0-2 United"."""
    found = re.match(r"^(.*\S)\s+\d+-\d+\s+(\S.*)$", str(facts.get("score_before", "")))
    return [found.group(1), found.group(2)] if found else []


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def mentions(sentence: str, team: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(team)}(?![\w])", sentence, re.I) is not None


def number_spans(text: str) -> list[tuple[float, int, int]]:
    return [(float(m.group(0).replace(",", ".")), m.start(), m.end()) for m in re.finditer(r"\d+(?:[.,]\d+)?", text)]


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


def verify(text: str, languages: list[str], facts: dict) -> tuple[list[dict], dict]:
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
        good = [c for c in claims if claim_ok(c, facts)]
        unclaimed, wrong_unit = [], []
        for value, is_pct in numbers_in(body):
            matches = [c for c in good if float(c["value"]) == value]
            if not matches:
                unclaimed.append(value)
            elif is_pct and not any(c["source_field"].endswith("_pct") for c in matches):
                wrong_unit.append(value)
        errors, warnings = style_issues(str(d.get("headline", "")), str(d.get("why", "")), str(d.get("lang")), good, facts)
        entry = {
            "lang": d.get("lang"),
            "keys_ok": all(isinstance(d.get(k), str) for k in ("headline", "why", "lang")),
            "bad_claims": [c for c in claims if not claim_ok(c, facts)],
            "unclaimed_numbers": unclaimed,
            "wrong_unit": wrong_unit + [v for v, _ in numbers_in(body) if 0 < v < 1],
            "unknown_names": sorted(set(names_ok(str(d.get("headline", "")), facts)) | set(names_ok(str(d.get("why", "")), facts))),  # each starts a sentence
            "length_ok": len(str(d.get("headline", "")).split()) <= HEADLINE_MAX_WORDS and len(str(d.get("why", "")).split()) <= WHY_MAX_WORDS,
            "style_errors": errors,
            "style_warnings": warnings,
        }
        entry["verified"] = entry["keys_ok"] and not entry["bad_claims"] and not entry["unclaimed_numbers"] and not entry["wrong_unit"] and not errors
        report["cards"].append(entry)
    report["verified"] = bool(drafts) and report["languages_ok"] and all(e["verified"] for e in report["cards"])
    return drafts, report


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
    return "The Verifier rejected the previous draft (" + " | ".join(lines)[:600] + "). Write it again following the rules."


# --- writers --------------------------------------------------------------------------------------

NB = "\u00a0"  # French typography: a no-break space before % and :
TEMPLATES = {
    "goal": {
        "en": ("{scorer} scores for {team}", "{team} go from {before}% to {after}% to win, up {swing} points."),
        "fr": ("{scorer} marque pour {team}", f"{{team}} passe de {{before}}{NB}% à {{after}}{NB}% de chances de gagner, soit {{swing}} points de mieux."),
        "es": ("Gol de {scorer} para el {team}", "El {team} pasa del {before}% al {after}% de opciones de ganar: {swing} puntos más."),
    },
    "save": {
        "en": ("Big save from {player}", "{team} stay on {before}% to win instead of {other}% had it gone in: the save is worth {value} points."),
        "fr": ("{player} s'interpose", f"{{team}} reste à {{before}}{NB}% de chances de gagner, contre {{other}}{NB}% si le tir était entré{NB}: l'arrêt vaut {{value}} points."),
        "es": ("Paradón de {player}", "El {team} se mantiene en el {before}% de opciones de ganar, frente al {other}% si hubiera entrado: la parada vale {value} puntos."),
    },
    "red_card": {
        "en": ("{player} sent off", "Down to ten men, {team} go from {before}% to {after}% to win."),
        "fr": ("{player} expulsé", f"Réduit à dix, {{team}} passe de {{before}}{NB}% à {{after}}{NB}% de chances de gagner."),
        "es": ("{player}, expulsado", "Con diez, el {team} pasa del {before}% al {after}% de opciones de ganar."),
    },
    "chance": {
        "en": ("Big chance for {team}", "A goal would have taken {team} from {before}% to {other}% to win."),
        "fr": ("Grosse occasion pour {team}", f"Un but aurait fait passer {{team}} de {{before}}{NB}% à {{other}}{NB}% de chances de gagner."),
        "es": ("Ocasión clara del {team}", "Un gol habría llevado al {team} del {before}% al {other}% de opciones de ganar."),
    },
}


class TemplateWriter:
    """Fixed sentences filled from the facts: no model, always verified."""

    name = "template"

    def write(self, card: dict, facts: dict, languages: list[str]) -> str:
        slug = card["team_name"].lower()
        values = {
            "team": card["team_name"], "scorer": facts.get("scorer"), "player": card["player"]["name"],
            "before": facts.get(f"{slug}_wp_before_pct", facts.get(f"{slug}_wp_after_save_pct")),
            "after": facts.get(f"{slug}_wp_after_pct"), "other": facts.get(f"{slug}_wp_if_goal_pct"),
            "swing": facts.get("swing_pts"), "value": facts.get("save_value_pts"),
        }
        fields = {"before": f"{slug}_wp_before_pct" if f"{slug}_wp_before_pct" in facts else f"{slug}_wp_after_save_pct", "after": f"{slug}_wp_after_pct",
                  "other": f"{slug}_wp_if_goal_pct", "swing": "swing_pts", "value": "save_value_pts"}
        out = []
        for lang in languages:
            headline, why = TEMPLATES[card["moment"]][lang]
            used = [k for k in fields if "{" + k + "}" in why]
            claims = [{"value": values[k], "source_field": fields[k]} for k in used]
            out.append({"headline": headline.format(**values), "why": why.format(**values), "lang": lang, "claims": claims})
        return json.dumps(out, ensure_ascii=False)


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

        credential = ChainedTokenCredential(ManagedIdentityCredential(), AzureCliCredential())  # managed identity in Azure, az login locally
        client = FoundryChatClient(project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], model=self.model, credential=credential)
        options: dict[str, Any] = {"max_tokens": max_tokens}  # GPT-5 models take no temperature
        effort = effort or os.environ.get("FOUNDRY_REASONING_EFFORT")
        if effort:
            options["reasoning"] = {"effort": effort}
        self.agent = Agent(client=client, name="writer", instructions=WRITER_INSTRUCTIONS, tools=[get_moment], default_options=options)

    async def write_async(self, card: dict, facts: tuple[dict, dict], languages: list[str], note: str | None = None) -> str:
        self.facts[card["id"]] = facts
        prompt = f"Write the cards for this request: {json.dumps({'card_id': card['id'], 'languages': languages})}"
        if note:
            prompt += f"\nProducer note: {note}"
        response = await self.agent.run(prompt)
        return response.text


@dataclass
class Draft:
    card_id: str
    version: int
    writer: str
    attempts: int
    texts: list[dict]
    report: dict
    machine_translated: list[str] = field(default_factory=list)


async def draft(card: dict, teams: dict[str, str], writer, languages: list[str] = list(LANGUAGES), calls_left: int = 2) -> Draft:
    """Writes and verifies a card: the model, one retry told what the Verifier rejected, then the template. `calls_left` caps the model calls this card may still make."""
    facts, meanings = card_facts(card, teams)
    attempts = 0
    note = None
    if not isinstance(writer, TemplateWriter):
        for _ in range(min(2, calls_left)):
            attempts += 1
            try:
                text = await (writer.write_async(card, (facts, meanings), languages, note=note) if note else writer.write_async(card, (facts, meanings), languages))
            except Exception as error:  # a model or network failure falls through to the template
                text = json.dumps({"error": f"{type(error).__name__}: {error}"[:300]})
            texts, report = verify(text, languages, facts)
            if report["verified"]:
                return Draft(card["id"], card["version"], writer.name, attempts, texts, report, sorted(MACHINE_TRANSLATED & set(languages)))
            note = feedback(report)
    texts, report = verify(TemplateWriter().write(card, facts, languages), languages, facts)
    return Draft(card["id"], card["version"], "template", attempts, texts, report, sorted(MACHINE_TRANSLATED & set(languages)))
