"""The Writer and the Verifier: from a Spotter card to on-air text in several languages, every number checked.

The code computes every number and sends it under an explicit field name (`card_facts`); the model only writes, and lists in `claims` each number it used with the field it comes from. The Verifier checks those claims, rejects any number nobody claimed, a percentage taken from a field that is not one, and any name that is not in the facts. A failed draft gets one retry, then a fixed template takes over, so a card is never blocked by the model.

The model runs on Microsoft Foundry through Agent Framework (`FoundryWriter`, a function tool `get_moment`); `TemplateWriter` needs no Azure and serves the tests and the fallback.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

LANGUAGES = ("en", "fr", "es")
MACHINE_TRANSLATED = {"es"}  # shown on air with a "machine-translated" label

WRITER_INSTRUCTIONS = """You write live football broadcast cards.

Facts: call the get_moment tool with the card_id to get the facts and the meaning of each field. Use ONLY those facts. Never invent a statistic, name or fact. If something you are asked for is not in the facts, write "not available" for it.

Each number has a field name that says what it means: never use a number with another meaning. For example, a win probability "if goal" is a chance of winning, never a chance of scoring. Probabilities are already percentages: write them with a % sign (34%, never 0.34). Values ending in _pts are percentage points: write them as points. Do not compute any new number.

Explain why the moment matters through the change in win probability, and credit the action that caused it (a save, not the shot it stopped). Write each language natively, with the same meaning in every language.

Reply with valid JSON only, no other text: a JSON array with one object per requested language:
{"headline": string (max 8 words), "why": string (max 25 words), "lang": string, "claims": [{"value": number, "source_field": string}]}
claims lists every number written in headline or why, with the field it comes from.

Example for another moment, facts {"minute": "81'", "score": "City 0-0 Town", "event": "Town goalkeeper save", "town_wp_after_save_pct": 22, "town_wp_if_goal_pct": 9, "save_value_pts": 13}, languages ["en", "es"]:
[{"headline": "Town keeper keeps them alive", "why": "The save kept Town at 22% to win instead of 9% if it had gone in: worth 13 points.", "lang": "en", "claims": [{"value": 22, "source_field": "town_wp_after_save_pct"}, {"value": 9, "source_field": "town_wp_if_goal_pct"}, {"value": 13, "source_field": "save_value_pts"}]}, {"headline": "El portero del Town lo mantiene vivo", "why": "La parada dejó al Town con un 22% de ganar, frente al 9% si entraba: vale 13 puntos.", "lang": "es", "claims": [{"value": 22, "source_field": "town_wp_after_save_pct"}, {"value": 9, "source_field": "town_wp_if_goal_pct"}, {"value": 13, "source_field": "save_value_pts"}]}]"""


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
    return [(float(m.group(1).replace(",", ".")), bool(m.group(2))) for m in re.finditer(r"(\d+(?:[.,]\d+)?)(\s?%)?", text.replace("\xa0", " ").replace(" ", " "))]


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
    report["languages_ok"] = sorted(d.get("lang") for d in drafts) == sorted(languages)
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
        entry = {
            "lang": d.get("lang"),
            "keys_ok": all(isinstance(d.get(k), str) for k in ("headline", "why", "lang")),
            "bad_claims": [c for c in claims if not claim_ok(c, facts)],
            "unclaimed_numbers": unclaimed,
            "wrong_unit": wrong_unit + [v for v, _ in numbers_in(body) if 0 < v < 1],
            "unknown_names": names_ok(body, facts),
            "length_ok": len(str(d.get("headline", "")).split()) <= 8 and len(str(d.get("why", "")).split()) <= 25,
        }
        entry["verified"] = entry["keys_ok"] and not entry["bad_claims"] and not entry["unclaimed_numbers"] and not entry["wrong_unit"]
        report["cards"].append(entry)
    report["verified"] = bool(drafts) and report["languages_ok"] and all(e["verified"] for e in report["cards"])
    return drafts, report


# --- writers --------------------------------------------------------------------------------------

TEMPLATES = {
    "goal": {
        "en": ("{scorer} scores for {team}", "{team} go from {before}% to {after}% to win: a swing of {swing} points."),
        "fr": ("{scorer} marque pour {team}", "{team} passe de {before} % à {after} % de chances de gagner : {swing} points de bascule."),
        "es": ("{scorer} marca para {team}", "{team} pasa del {before}% al {after}% de opciones de ganar: {swing} puntos de cambio."),
    },
    "save": {
        "en": ("{player} keeps {team} ahead", "The save kept {team} at {before}% to win instead of {other}% if it had gone in: worth {value} points."),
        "fr": ("{player} garde {team} devant", "L'arrêt laisse {team} à {before} % de victoire, contre {other} % si le tir était entré : {value} points."),
        "es": ("{player} sostiene a {team}", "La parada dejó a {team} con un {before}% de ganar, frente al {other}% si entraba: vale {value} puntos."),
    },
    "red_card": {
        "en": ("{player} sent off", "{team} drop from {before}% to {after}% to win with ten men."),
        "fr": ("{player} expulsé", "{team} passe de {before} % à {after} % de chances de gagner à dix."),
        "es": ("{player}, expulsado", "{team} baja del {before}% al {after}% de opciones de ganar con diez."),
    },
    "chance": {
        "en": ("Big chance for {team}", "A goal would have taken {team} from {before}% to {other}% to win."),
        "fr": ("Grosse occasion pour {team}", "Un but aurait fait passer {team} de {before} % à {other} % de chances de gagner."),
        "es": ("Ocasión clara para {team}", "Un gol habría llevado a {team} del {before}% al {other}% de opciones de ganar."),
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

    def __init__(self, model: str | None = None, effort: str | None = None, max_tokens: int = 4000) -> None:
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


async def draft(card: dict, teams: dict[str, str], writer, languages: list[str] = list(LANGUAGES)) -> Draft:
    """Writes and verifies a card: the model, one retry, then the template."""
    facts, meanings = card_facts(card, teams)
    attempts = 0
    if not isinstance(writer, TemplateWriter):
        for _ in range(2):
            attempts += 1
            try:
                text = await writer.write_async(card, (facts, meanings), languages)
            except Exception as error:  # a model or network failure falls through to the template
                text = json.dumps({"error": f"{type(error).__name__}: {error}"[:300]})
            texts, report = verify(text, languages, facts)
            if report["verified"]:
                return Draft(card["id"], card["version"], writer.name, attempts, texts, report, sorted(MACHINE_TRANSLATED & set(languages)))
    texts, report = verify(TemplateWriter().write(card, facts, languages), languages, facts)
    return Draft(card["id"], card["version"], "template", attempts, texts, report, sorted(MACHINE_TRANSLATED & set(languages)))
