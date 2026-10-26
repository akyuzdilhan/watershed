"""The commentary duo written by two agents after the card.

Wren Calder (the heart and the story) and Joss Ferrier (the eye and the numbers) are two Agent Framework agents with the personas of `worker.duo`, each with its own read-only tools from the desk's MCP server (`worker.mcp_server`). A GroupChat workflow gives them the floor in a running order fixed by code, with no model call to orchestrate. Every turn goes through the Verifier before the other agent hears it (`TurnGate`): every number claimed from a fact of the card or a tool result of this exchange, units, team names, French and Spanish rules, no name that is not in those facts (at the start of a sentence too, where only a common word of the language may stand instead), no number word without its fact (three, tercera, dix-sept, hat-trick, brace...), no tool number without the club or player it is about. One retry told what was rejected; a second failure ends the job and the card keeps its template duo. Jobs run on a thread and an event loop of their own (`DuoRunner`), one at a time, so the card is never delayed; each reserves the runs of its turns when it is submitted.
"""

import asyncio
import concurrent.futures
import contextlib
import copy
import json
import logging
import os
import queue
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass

from agent_framework import Agent, AgentMiddleware, AgentResponse, BaseChatClient, ChatResponse, Content, FunctionInvocationLayer, MCPStdioTool, Message
from agent_framework.orchestrations import GroupChatBuilder
from mcp import Client

from worker.duo import ALLOWED_NAMES, CARDINALS, EYE, HEART, LINES, ORDINALS, ORDINALS_BEFORE_NOUN, PERSONAS
from worker.insight_texts import FEAT
from worker.insight_texts import T as INSIGHT_LINES
from worker.mcp_server import LAST_TIME_KINDS, build_server, scalar
from worker.writer import (COUNT_WORDS, EXAMPLE_DUO, EXAMPLE_FACTS, INSIGHT_TEMPLATES, LOW_STAKE_TEMPLATES, SEASON_LINES, TEMPLATES, card_facts, claim_ok, mentions,
                           names_ok, normalise, number_spans, sentences, verify_duo)

log = logging.getLogger(__name__)

DUO_MOMENTS = ("goal", "save", "red_card", "chance")
WREN_TOOLS = ("get_match_state", "get_storyline")
JOSS_TOOLS = ("player_history", "last_time", "head_to_head", "club_history")
DUO_LIMITS = {"max_iterations": 2, "max_function_calls": 1, "max_duration_seconds": 12}  # per agent run: one tool call, at most two model requests
DUO_MAX_TOKENS = 1500
DUO_RETRIES = 2  # per job, at most one per turn
BIG_MOMENT_PTS = 30  # as the template duo: a moment worth this many points gets a third line

# --- number words: a word that says a number needs a whole-number fact of that value ---------------------------------
ES_ORDINALS = {w: i + 1 for i, w in enumerate(ORDINALS["es"]) if i >= 2} | {
    "undécimo": 11, "duodécimo": 12, "decimotercero": 13, "decimocuarto": 14, "decimoquinto": 15, "decimosexto": 16, "decimoséptimo": 17, "decimoctavo": 18,
    "decimonoveno": 19, "vigésimo": 20, "trigésimo": 30}
EXTRA_NUMBER_WORDS = {
    "en": {"twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
           "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000, "dozen": 12,
           "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
           "twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50, "hundredth": 100},
    "fr": {"douze": 12, "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16, "vingt": 20, "vingts": 20, "trente": 30, "quarante": 40, "cinquante": 50, "soixante": 60,
           "cent": 100, "cents": 100, "mille": 1000, "douzaine": 12, "onzième": 11, "douzième": 12, "treizième": 13, "quatorzième": 14, "quinzième": 15, "seizième": 16,
           "vingtième": 20, "trentième": 30, "quarantième": 40, "cinquantième": 50, "centième": 100},
    "es": {"doce": 12, "trece": 13, "catorce": 14, "quince": 15, "dieciséis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiuno": 21,
           "veintiún": 21, "veintiuna": 21, "veintidós": 22, "veintitrés": 23, "veinticuatro": 24, "veinticinco": 25, "veintiséis": 26, "veintisiete": 27, "veintiocho": 28,
           "veintinueve": 29, "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90, "cien": 100, "ciento": 100,
           "doscientos": 200, "trescientos": 300, "cuatrocientos": 400, "quinientos": 500, "mil": 1000, "docena": 12, "tercer": 3}
          | ES_ORDINALS | {w[:-1] + "a": n for w, n in ES_ORDINALS.items()}}  # el tercer gol, la tercera vez
# What a player's goals in one match are called: hat-trick in every language (written "hattrick" once normalised), brace, doublé, triplete, póker...
FEAT_WORDS = {lang: {"hattrick": 3} | {w: n for n, w in FEAT[lang].items() if w.isalpha()} | ({"poker": 4, "repoker": 5} if lang == "es" else {}) for lang in FEAT}
# Left out on purpose: one, two (un, deux) and ten (dix, diez: "ten men"), eleven (onze, once: "le onze de départ"), first and second (also a unit of time).
NUMBER_WORDS = {lang: COUNT_WORDS[lang] | EXTRA_NUMBER_WORDS[lang] | FEAT_WORDS[lang] for lang in COUNT_WORDS}
# What a number written in several words is made of, the words left out above included: twenty-two, dix-sept, vingt et un, treinta y dos.
NUMBER_PARTS = {lang: {w: i + 1 for words in (CARDINALS[lang], ORDINALS[lang], ORDINALS_BEFORE_NOUN[lang]) for i, w in enumerate(words)} | EXTRA_NUMBER_WORDS[lang]
                | {"en": {"eleven": 11}, "fr": {"onze": 11, "une": 1, "unième": 1}, "es": {"once": 11, "un": 1, "una": 1}}[lang] for lang in CARDINALS}
TENS = {"en": "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety", "fr": "vingt|trente|quarante|cinquante|soixante",
        "es": "treinta|cuarenta|cincuenta|sesenta|setenta|ochenta|noventa"}
JOINED = {  # "twenty two", "vingt et un", "treinta y dos": one number
    "en": re.compile(rf"\b({TENS['en']}) (one|two|three|four|five|six|seven|eight|nine|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)\b"),
    "fr": re.compile(rf"\b({TENS['fr']}) et (un|une|unième|onze|onzième)\b"),
    "es": re.compile(rf"\b({TENS['es']}) y (uno|una|un|dos|tres|cuatro|cinco|seis|siete|ocho|nueve)\b")}
HAT_TRICK = {"en": re.compile(r"\bhat[\s-]?trick\b"), "fr": re.compile(r"\bhat[\s-]?trick\b|\bcoup du chapeau\b"), "es": re.compile(r"\bhat[\s-]?trick\b")}

# --- names: at the start of a sentence, a capitalised word must be a common word of the language or a name of the facts ------------------
COMMON_WORDS_TEXT = {  # words a spoken line may start with, besides the words of the templates and the number words
    "en": """a about above absolute absolutely absurd across after again against ago ah ahead all almost alone along already also although always amazing amazingly among an and
             another any anybody anyone anything anyway are around as astonishing astonishingly at away awful back bad basically be beautiful because been before behind being below
             best better between beyond big both brave bravo brilliant brutal but by calm can can't cannot careful certainly class classic clearly clinical come composed cool could
             couldn't crazy credit critical crucial crucially curiously dear decisive deep delightful desperate devastating did didn't do does doesn't don't done down dramatic during
             each early easily easy either electric elegant else enormous enough epic essentially even eventually ever every everybody everyone everything evidently exactly excellent
             exceptional exquisite extraordinary fabulous fair fantastic far fascinating fearless few finally fine fittingly for forget fortunately frankly frantic from full funnily
             genius get glorious glory gone good goodness gorgeous gosh great had half hard has hasn't have haven't he he's heartbreak heartbreaking heavens her here here's heroic hers herself
             hey high him himself his historic historically history hold honestly horrible how however huge if imagine importantly impressive in incredible incredibly indeed
             inevitable inevitably inside instead interesting interestingly into ironically is isn't it it's its itself joy just keep last late later least less let let's lethal like
             listen little long look lovely low luckily lucky madness magic magnificent majestic many marvellous massive maybe me meanwhile might mind miraculous more moreover most
             much must my myself near nearly neither nerveless never nevertheless next nice no nobody none nor not notably nothing now nowhere obviously of off often oh ok okay on
             once only onto or other others otherwise our ours out outrageous outside over painful perfect perfectly perhaps please plenty poor precise precisely predictably priceless
             pure quality quick quickly quiet quite rather real really relentless remarkable remarkably remember ridiculous right ruthless sadly same say scenes see sensational
             seriously several shall sharp she she's shocking should shouldn't simple simply since smart so some somebody somehow someone something sometimes soon sorry special
             spectacular splendid statistically steady still stop strange strangely striking stunning sublime such sudden superb sure surely surprisingly sweet take technically
             tellingly terrific textbook than thank thankfully thanks that that's the their theirs them then there there's therefore these they they're they've thing this those though
             thrilling through thus tidy time to today together tonight too top totally tremendous truly typical typically ultimately unbelievable unbelievably uncanny under
             unfortunately unless unlucky unreal unstoppable unsurprisingly until up upon us very vintage wait was wasn't watch we we're we've well were weren't what what's whatever
             when where where's whether which while who who's whole whose why wild will with within without won't wonderful worse worst would wouldn't wow yeah yep yes yet you you're
             you've your yours""",
    "fr": """absolument admirez ah ai ainsi allez alors après attendez attends attention au aucun aucune aujourd'hui aussi autant autre autres aux avant avec avez avoir bah beau
             beaucoup belle bien bientôt bon bonne bravo bref c car carrément ce ceci cela celle celui certes ces cet cette chacun chapeau chaque chez ciel clairement combien comme
             comment contre curieusement d dans de demain depuis derrière des devant dingue dire dis dit doit donc dont drôle du dès décidément définitivement déjà désormais
             effectivement elle elles en encore enfin ensuite entre es est et eux exactement exceptionnel exceptionnelle extraordinaire fabuleux facile fallait fantastique faut fin
             finalement folie forcément formidable fou franchement grand grande gros grosse grâce génial géniale hein heureusement hier historique historiquement hop hé hélas ici il
             ils imaginez imparable implacable impossible inarrêtable incroyable incroyablement inévitablement j jamais je joli jolie jusqu juste l la laissez le les leur leurs
             logiquement lorsqu lui là m ma madame magistral magistrale magnifique mais malgré malheureusement me merci mes messieurs mieux moi moins mon monsieur même n naturellement
             ne ni non normalement nos notre nous oh on ou ouah ouf ouh oui où par parce pardon parfois parmi pas personne petit petite peu peut pfiou plus plutôt pour pourquoi
             pourtant presqu presque prodigieux précisément puis puisqu qu quand quant que quel quelle quelles quelqu quelque quelques quels qui quoi rare rarement remarquable rien s
             sa sacré sans se selon sensationnel sensationnelle ses seul seule seulement si simplement sinon soit somptueuse somptueux son soudain sous souvent spectaculaire splendide
             statistiquement sublime superbe sur surtout sûrement t ta tant te tellement tenez terrible tes toi ton totalement toujours tous tout toute toutes tranquille trop très tu
             vas visiblement vite voici voilà vos votre vous voyez voyons vraiment waouh y à ça écoute écoutez également énorme énormément étonnamment évidemment êtes être ô""",
    "es": """a abajo absolutamente adelante afortunadamente ahora ahí al algo alguien alguna alguno algunos algún allá allí anda ante antes aquel aquella aquello aquí asombroso así
             atención atentos aunque ay aún bastante bestial bien bravo brutal buen buena bueno bárbaro cada calma caramba casi cierto claramente claro como con contra cual cuando
             cuidado curiosamente cuál cuándo cuánta cuántas cuánto cuántos cómo de decisivo definitivamente del desde después donde durante dónde e efectivamente eh el ella ellas
             ello ellos en enorme enormemente entonces entre era eran eres es esa esas escandaloso escucha escuchen ese eso esos espectacular esta estas este esto estos estoy está
             están evidentemente exactamente exacto excelente extraordinario fantástico fin finalmente fue fueron fíjate fíjense genial gol golazo gracias gran grande ha hace han
             hasta hay he hemos históricamente histórico hoy imaginen imparable impresionante increíble increíblemente inevitablemente ir jamás justamente la lamentablemente las le
             les lo locura los luego madre magnífico mal maravilla maravilloso me mejor menos menuda menudo mi mientras milagro mira mire miren mis mucha mucho muchos muy más mí nada
             nadie ni ninguna ninguno ningún no nos nosotros nuestra nuestro nuestros nunca o obviamente oh ojo ole olé otra otras otro otros para pedazo perfecto pero poco pocos por
             porque precioso precisamente pues que quien quizá quizás quién quiénes qué realmente se según sencillamente sensacional señoras señores si siempre simplemente sin sino
             soberbio sobre solo son su sublime sus sé sí sólo también tampoco tan tanto te tenemos tiene tienen toda todas todavía todo todos tras tremenda tremendo triste tu tus tú
             uf un una unas uno unos usted ustedes va vamos van vaya venga ver y ya yo él"""}
SENTENCE_START = re.compile(r"(?:^|(?<=[.!?:]\s)|(?<=[¡¿]))([A-ZÀ-Ý][\wÀ-ÿ'-]+)")  # the words names_ok leaves out: first of the text, after . ! ? : and after ¡ ¿


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def _template_words(lang: str) -> set[str]:
    """The lower-case words of the duo lines, card templates and persona tics of a language, placeholders and capitalised words inside a sentence (names) left out."""
    out = set()
    sources = [LINES[k][lang] for k in LINES] + [TEMPLATES[m][lang] for m in TEMPLATES] + [LOW_STAKE_TEMPLATES[lang], INSIGHT_TEMPLATES[lang], SEASON_LINES[lang]]
    sources += [INSIGHT_LINES[f][lang] for f in INSIGHT_LINES] + [[*p.voices[lang].tics, *p.voices[lang].vocabulary] for p in PERSONAS.values()]
    for text in _strings(sources):
        text = normalise(re.sub(r"\{[^}]*\}", " ", text))
        for m in re.finditer(r"[\wÀ-ÿ]+(?:['’][\wÀ-ÿ]+)*", text):
            before = text[:m.start()].rstrip(" \"'“”«»‘’(")
            if m.group(0)[:1].isupper() and before and before[-1] not in ".!?:;¡¿":
                continue
            out.add(m.group(0).lower().replace("’", "'"))
    return out


COMMON_WORDS = {lang: frozenset(COMMON_WORDS_TEXT[lang].split()) | _template_words(lang) | set(NUMBER_WORDS[lang]) | set(NUMBER_PARTS[lang]) for lang in COMMON_WORDS_TEXT}

DUO_RULES = """How the booth works. The producer's desk sends you a moment of the match with its facts (the briefing) and the running order. You and your partner speak in turns. At each turn you write ONE spoken line: the same remark in each requested language, written natively as a commentator of that country would say it, not translated word for word.

Facts. Use only the facts of the briefing and the results of your own tools. Never invent a statistic, a name or an event, and never compute a new number. Each fact has a field name and a meaning: never use a number with another meaning.

Numbers. Write numbers in digits, exactly as given: whole numbers, no sign, no decimals. Every number in a line is listed in that line's claims as {"value": number, "source_field": field}. A season such as 2020/21 is two numbers (2020 and 21) and a score such as 1-3 is two numbers: claim each with the field that holds it. A number word (three, trois, tres, third, tercera, dix-sept, and hat-trick, brace, doublé, triplete...) is allowed only when a whole-number fact has that value. Fields ending in _pct are percentages: write the % sign (French: a space before it, "34 %"). Fields ending in _pts are percentage points of a team's chance of winning: write the word points (French points, Spanish puntos) after the number. The word points means nothing else.

Teams. A sentence that gives a chance of winning names the team it belongs to. French: club names without article ("Varleigh", "pour Yarnford", never "le Varleigh", "l'Yarnford", "d'Yarnford"), never swing or momentum. Spanish: with the article ("el Varleigh", "del Yarnford"), never swing or momentum.

History. Facts from the league history (fields starting with player_, club_, h2h_, last_, story_, and the history_ fields) stop at the season in history_through: the current season and tonight's match are not in them; say "before this season" when it matters. Say how rare something is in seasons, anchored on the club or the player ("for the first time since 2015/16", "6 seasons ago"), never as a number of matches.

Style. Spoken, not written: one or two short sentences, at most 25 words. Do not copy the headline or the why already on screen (on_screen), do not start like the openings in avoid, and never say a tic listed in tics_used. Capitalised names: only those in the facts, in your tool results, Wren and Joss, at the start of a sentence too. Never name or imitate a real commentator or pundit; keep it broadcast-safe, never insulting.

Reply with valid JSON only, no other text: {"lines": [{"lang": "en", "line": string, "claims": [{"value": number, "source_field": string}]}, ...one object per requested language, in the order of the briefing]}."""

ROLE_TEXT = {"react": "React to the moment as it happens: the emotion and the story. A number is not needed.",
             "detail": "Bring one number or one detail nobody noticed, from the facts of the moment or from one tool result worth saying.",
             "answer": "Answer your partner's last line with feeling; you may pick up a number they gave, claimed with its field.",
             "ask_table": "Ask Joss, in a few words, what this result does to the league table.",
             "table": "Answer Wren: what the score does to the table, from the season_ facts of the moment; claim at least one season_ field. No tool is needed."}

TOOL_TEXT = {"get_match_state": "get_match_state(card_id): the match so far at this moment: score, the goals that count in order, red cards, the largest deficit of each club.",
             "get_storyline": "get_storyline(match_id): the pre-match storyline: league positions, what is at stake in %, form, head-to-head, players to watch.",
             "player_history": "player_history(name, club): a player's league career before this season: goals, appearances, appearances off the bench, first season, clubs, best season.",
             "club_history": "club_history(club): titles, seasons in the league, best finish, last title.",
             "head_to_head": "head_to_head(club_a, club_b): league meetings of two clubs: how many, wins, draws, first and last meeting, biggest wins.",
             "last_time": "last_time(kind, club): the last time something happened in the league history and how rare it is; kind is hat_trick, hat_trick_by_substitute, brace_by_substitute or comeback_win_from_two_down."}

TURN_MARK = "Your turn: "
WITHHELD = "(withheld by the Verifier)"
REASONS = {"bad_claims": "numbers", "unclaimed_numbers": "numbers", "wrong_unit": "numbers", "number_words": "numbers", "style": "style", "unknown_names": "names",
           "table": "table", "subject": "subject", "format": "format"}


@dataclass(frozen=True)
class DuoSpec:
    """Everything a duo job needs, copied from the card when it is submitted: the duo thread never reads the desk's state."""
    card_id: str
    version: int
    match_id: str
    moment: str
    languages: tuple[str, ...]
    facts: dict
    meanings: dict
    plan: tuple[tuple[str, str], ...]  # (speaker, role) per turn
    template: tuple[dict, ...]  # the card's template duo
    on_screen: dict  # lang -> {headline, why}
    avoid: dict  # what the match already said (PhraseMemory.avoid)
    player: str | None
    tools: dict  # speaker -> MCP tool names
    submitted_at: float  # time.monotonic()


@dataclass
class DuoResult:
    """What a job gives back to the desk: the verified lines, or why there are none."""
    card_id: str
    version: int
    match_id: str
    ok: bool
    reason: str | None
    lines: list[dict]
    tool_facts: dict  # field -> {value, tool, args}, the tool facts the lines claim
    trace: dict


# --- the exchange, as data -----------------------------------------------------------------------------

def duo_plan(facts: dict, template_lines) -> tuple:
    """The running order: four turns when the template duo tells the table (a season_ claim), else two, three for a big moment or a hat-trick. verify_duo then gets 2 to 4 alternating lines per language."""
    if any(isinstance(c, dict) and str(c.get("source_field", "")).startswith("season_") for l in template_lines for c in l.get("claims") or []):
        return ((HEART, "react"), (EYE, "detail"), (HEART, "ask_table"), (EYE, "table"))
    plan = ((HEART, "react"), (EYE, "detail"))
    big = max(abs(facts.get("swing_pts") or 0), abs(facts.get("save_value_pts") or 0)) >= BIG_MOMENT_PTS
    if big or (isinstance(facts.get("scorer_match_goals"), int) and facts["scorer_match_goals"] >= 3):
        plan += ((HEART, "answer"),)
    return plan


def duo_spec(card: dict, teams: dict[str, str], languages, memory=None, tools: dict | None = None) -> DuoSpec:
    """The job of a written card: its facts, its template duo, what is on screen and what the match already said, deep copies all."""
    facts, meanings = card_facts(card, teams)
    template = copy.deepcopy([l for l in card.get("duo") or [] if isinstance(l, dict)])
    on_screen = {t.get("lang"): {"headline": t.get("headline"), "why": t.get("why")} for t in card.get("texts") or [] if isinstance(t, dict)}
    tools = {HEART: WREN_TOOLS, EYE: JOSS_TOOLS} if tools is None else tools
    return DuoSpec(card_id=card["id"], version=card.get("version", 1), match_id=card["match_id"], moment=card["moment"], languages=tuple(languages),
                   facts=copy.deepcopy(facts), meanings=copy.deepcopy(meanings), plan=duo_plan(facts, template), template=tuple(template),
                   on_screen=copy.deepcopy(on_screen), avoid=copy.deepcopy(memory.avoid(list(languages))) if memory is not None else {},
                   player=(card.get("player") or {}).get("name"), tools={k: tuple(v) for k, v in tools.items()}, submitted_at=time.monotonic())


def tools_text(speaker: str, names) -> str:
    """The tools paragraph of a persona's instructions."""
    if not names:
        return "You have no tools for this moment: use the facts of the briefing."
    text = "Your tools:\n" + "\n".join(f"- {TOOL_TEXT[n]}" for n in names)
    text += "\nCall at most one tool in a turn, only when its fact makes a better line; the facts of the moment are already in the briefing."
    if speaker == EYE:
        text += " Quote at most one history number in a line."
    return text


def persona_instructions(speaker: str, languages: list[str], tools) -> str:
    """The instructions of Wren or Joss: who they are, their voice in each language, the booth rules, their tools, and one worked turn."""
    p, o = PERSONAS[speaker], PERSONAS[HEART if speaker == EYE else EYE]
    voice = "\n".join(f'- {lang}: vocabulary {", ".join(v.vocabulary)}; tics, once per match each: {" | ".join(v.tics)}; sounds like: "{v.sample}"'
                      for lang in languages for v in (p.voices[lang],))
    example = {"lines": [{"lang": l["lang"], "line": l["line"], "claims": l["claims"]} for l in EXAMPLE_DUO if l["speaker"] == speaker and l["lang"] in languages]}
    return (f"You are {p.name} ({p.role}), one of the two fictional commentators of a live football broadcast; your partner is {o.name}, you call each other {o.short} and {p.short}.\n"
            f"Who you are: {p.background} You notice: {'; '.join(p.notices)}. Rhythm: {p.rhythm} With {o.short}: {p.interaction} You avoid: {', '.join(p.avoids)}.\n"
            f"Your voice:\n{voice}\n\n{DUO_RULES}\n\n{tools_text(speaker, tools)}\n\n"
            f"Example of one turn for another moment, facts {json.dumps(EXAMPLE_FACTS, ensure_ascii=False)}: {json.dumps(example, ensure_ascii=False)}")


def briefing(spec: DuoSpec) -> str:
    """The first message of the exchange, heard by both agents."""
    return "Moment for the commentary duo: " + json.dumps({
        "card_id": spec.card_id, "match_id": spec.match_id, "moment": spec.moment, "languages": list(spec.languages),
        "running_order": [{"slot": i, "speaker": s, "role": r} for i, (s, r) in enumerate(spec.plan)],
        "facts": spec.facts, "field_meanings": spec.meanings, "on_screen": spec.on_screen, "avoid": spec.avoid}, ensure_ascii=False)


def turn_prompt(spec: DuoSpec, slot: int) -> str:
    """What the desk tells the agent whose turn it is."""
    s, r = spec.plan[slot]
    return f'{TURN_MARK}{json.dumps({"slot": slot, "of": len(spec.plan), "speaker": s, "role": r})}. {ROLE_TEXT[r]} Reply with the JSON object only, one line per language: {", ".join(spec.languages)}.'


def retry_note(problems: list[str]) -> str:
    """The retry of a turn: what the Verifier rejected."""
    return ("The Verifier rejected your line (" + " | ".join(problems)[:600] + "). Write the same turn again as the same JSON object, with only the facts of the briefing and "
            "of your tool results, every number claimed with its exact field.")


# --- the Verifier, line by line ------------------------------------------------------------------------

def tokens_of(text: str, lang: str | None = None) -> list[str]:
    """The lower-case words of a text, a hyphenated group kept whole ("dix-sept", "six-pointer"), hat-trick written "hattrick" and, for `lang`, "vingt et un" or "treinta y dos" joined into one group."""
    text = HAT_TRICK.get(lang, HAT_TRICK["en"]).sub("hattrick", normalise(text).lower())
    if lang in JOINED:
        text = JOINED[lang].sub(r"\1-\2", text)
    return re.findall(r"[\wÀ-ÿ]+(?:-[\wÀ-ÿ]+)*", text)


def compound(parts: list[str], lang: str) -> int | None:
    """A number written in hyphenated words (twenty-two, dix-sept, quatre-vingt-dix, vingt-et-unième, deux-cents), or None when the words are not one number ("one-two")."""
    values = NUMBER_PARTS.get(lang, {})
    total, prev, i = 0, None, 0
    while i < len(parts):
        if parts[i] == "et" and prev is not None:
            i += 1
            continue
        if lang == "fr" and parts[i] == "quatre" and parts[i + 1:i + 2] in (["vingt"], ["vingts"]):
            value, i = 80, i + 2
        elif parts[i] in values:
            value, i = values[parts[i]], i + 1
        else:
            return None
        if value in (100, 1000):
            total = max(total, 1) * value
        elif prev is None or (prev % 10 == 0 and value < prev):  # tens, then what follows them
            total += value
        else:
            return None
        prev = value
    return total


def number_words_in(text: str, lang: str) -> list[tuple[str, int]]:
    """The number words of a text with their values: a word (three, tercera, hat-trick, doublé) or a number in several words, read whole (dix-sept is 17, not 7)."""
    words, out = NUMBER_WORDS.get(lang, {}), []
    for token in tokens_of(text, lang):
        parts = token.split("-")
        value = compound(parts, lang) if len(parts) > 1 else None
        if value is not None:
            out.append((token, value))
        else:
            out += [(p, words[p]) for p in parts if p in words]
    return out


def fact_words(facts: dict) -> set[str]:
    """The words of the text facts, each hyphenated group whole and in parts."""
    return {w for v in facts.values() if isinstance(v, str) for token in tokens_of(v) for w in (token, *token.split("-"))}


def unbacked_number_words(text: str, lang: str, facts: dict) -> list[str]:
    """Number words (three, tercer, dix-sept, hat-trick...) with no whole-number fact of that value, and not a word of a text fact ("six" of "six-pointer")."""
    counts = {v for v in facts.values() if isinstance(v, int) and not isinstance(v, bool)}
    in_facts = fact_words(facts)
    return sorted({"hat-trick" if w == "hattrick" else w for w, n in number_words_in(text, lang) if n not in counts and w not in in_facts})


def unknown_names(text: str, lang: str, facts: dict) -> list[str]:
    """Capitalised words that are neither in the facts (tool results included) nor Wren, Joss or VAR: those names_ok finds, and here also the first word of a sentence, unless it is a common word of the language ("Even", "Encore", "Hasta")."""
    allowed = " ".join(str(v) for v in facts.values())
    common = COMMON_WORDS.get(lang, frozenset())
    found = set(names_ok(text, facts, ALLOWED_NAMES))
    for m in SENTENCE_START.finditer(text):
        word = re.sub(r"['’]s?$", "", m.group(1))  # Scarpetta's
        if word in allowed or word in ALLOWED_NAMES or word.lower() in common:
            continue
        head, *rest = re.split(r"['-]", word)  # L'Yarnford, Dix-sept
        if (head in allowed or head.lower() in common) and all(p[:1].islower() or p in allowed or p in ALLOWED_NAMES or p.lower() in common for p in rest):
            continue
        found.add(word)
    return sorted(found)


def subject_problem(lang: str, text: str, claim: dict, subjects: list[str]) -> str | None:
    """A tool number told without what it is about: the first sentence holding the claimed number must name one of the fact's subjects (a player's surname is enough)."""
    names = set(subjects) | {s.split()[-1] for s in subjects if len(s.split()) > 1}
    value = float(claim["value"])
    for sentence in sentences(normalise(text)):
        if any(v == value for v, _, _ in number_spans(sentence)):
            if any(mentions(sentence, n) for n in names):
                return None
            return f"{lang}: subject: {claim.get('source_field')} is about {' or '.join(subjects)}: name it in the same sentence"
    return None


LEAGUE_WIDE_FIELDS = frozenset(f"{kind}_seasons_with_one" for kind in LAST_TIME_KINDS)  # last_time without a club: seasons of the whole league


def off_air_field(name: str) -> bool:
    """A tool fact the agents may read but never tell: a count of times in the history (a number of matches, in effect) or a rarity of the whole league, not anchored on a club or a player."""
    return name.endswith("_count") or name in LEAGUE_WIDE_FIELDS


def line_problems(line: dict, facts: dict, role: str | None = None, subjects: dict | None = None) -> list[str]:
    """What the Verifier rejects in one duo line, as "<lang>: <category>: <detail>": the Writer's duo checks (claims, units, style), and here also a name that is not in the facts (at the start of a sentence too), a number word without its fact, a table turn without a season_ claim, and a tool number told without its subject (`subjects`: field -> the clubs or players its tool result is about)."""
    lang = str(line.get("lang"))
    facts = {k: v for k, v in facts.items() if not off_air_field(k)}  # a claim on an off-air fact is a bad claim, and backs no number word
    e = verify_duo([line], [lang], facts)["lines"][0]
    problems = [] if e["keys_ok"] else [f"{lang}: format: empty line"]
    for k in ("bad_claims", "unclaimed_numbers", "wrong_unit"):
        if e[k]:
            problems.append(f"{lang}: {k}: {json.dumps(e[k], ensure_ascii=False)}")
    problems += [f"{lang}: style: {i['rule']}: {i['text']}" for i in e["style_errors"]]
    names = unknown_names(str(line.get("line", "")), lang, facts)
    if names:
        problems.append(f"{lang}: unknown_names: {names}: only the names of the facts and of your tool results")  # an error for the agents; only a warning for the Writer
    words = unbacked_number_words(str(line.get("line", "")), lang, facts)
    if words:
        problems.append(f"{lang}: number_words: {words}")
    claims = line.get("claims") if isinstance(line.get("claims"), list) else []
    if role == "table" and not any(claim_ok(c, facts) and str(c.get("source_field", "")).startswith("season_") for c in claims):
        problems.append(f"{lang}: table: claim at least one season_ field")
    for c in claims:
        about = (subjects or {}).get(c.get("source_field")) if isinstance(c, dict) else None
        if about and claim_ok(c, facts):
            problem = subject_problem(lang, str(line.get("line", "")), c, about)
            if problem:
                problems.append(problem)
    return problems


def relied_on(lines: list[dict], ledger: dict) -> list[str]:
    """The tool facts a duo's lines rely on: claimed, holding a capitalised word of a line (a name), or backing a number word. With the card's facts, they are what the duo is checked against again."""
    claimed = {c.get("source_field") for l in lines for c in l.get("claims") or [] if isinstance(c, dict)}
    capitalised = {re.sub(r"['’]s?$", "", w) for w in re.findall(r"[A-ZÀ-Ý][\wÀ-ÿ'-]+", " ".join(str(l.get("line", "")) for l in lines))}  # Delacroix's, as names_ok reads it
    capitalised |= {p for w in capitalised for p in re.split(r"['-]", w) if p[:1].isupper()}  # L'Yarnford, as unknown_names reads it
    said = {pair for l in lines for pair in number_words_in(str(l.get("line", "")), str(l.get("lang")))}
    number_words, values = {w for w, _ in said}, {n for _, n in said}

    def relied(k: str, v) -> bool:
        if k in claimed:
            return True
        if isinstance(v, str):
            return any(w in v for w in capitalised) or bool(number_words & fact_words({k: v}))
        return isinstance(v, int) and not isinstance(v, bool) and v in values

    return [k for k, v in ledger.items() if relied(k, v)]


def check_duo(lines: list[dict], languages, facts: dict, plan) -> list[str]:
    """The final check of an exchange: 2 to 4 alternating lines per language in the running order, and every line as at its turn."""
    report = verify_duo(lines, list(languages), facts)
    problems = [f"format: {lang}: 2 to 4 lines, alternating wren and joss" for lang, v in report["languages"].items() if not (v["count_ok"] and v["alternates"] and v["speakers_ok"])]
    for lang in languages:
        for slot, l in enumerate(x for x in lines if isinstance(x, dict) and x.get("lang") == lang):
            if slot >= len(plan) or l.get("speaker") != plan[slot][0]:
                problems.append(f"format: {lang}: line {slot} not in the running order")
            problems += line_problems(l, facts, plan[slot][1] if slot < len(plan) else None)
    return problems


def reason_of(problems: list[str]) -> str:
    """The fallback reason of the first problem: numbers, style, names, table, subject or format."""
    parts = problems[0].split(": ") if problems else ["format"]
    category = parts[0] if parts[0] == "format" or len(parts) < 2 else parts[1]
    return REASONS.get(category, "format")


def leaf_error(e: BaseException) -> BaseException:
    """The first exception at the bottom of an exception group: a model error under mcp.Client comes up wrapped in one."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


def turns_line(trace: dict) -> str:
    """One line for a log or the audit: who spoke, the tools they called, runs and time."""
    return ", ".join(t["speaker"] + (f" ({', '.join(x['name'] for x in t['tools'])})" if t.get("tools") else "") for t in trace["turns"]) + f": {trace['runs']} runs, {trace['ms'] / 1000:.1f} s"


def duo_summary(card: dict) -> str:
    """Who wrote a card's duo, for the worker's output."""
    trace = card.get("duo_trace") or {}
    if str(card.get("duo_writer", "")).startswith("agents:"):
        return turns_line(trace)
    written = "template" if card.get("duo_writer") == "template" else "Writer"
    return f"{written} ({trace['reason']})" if trace.get("reason") else written


# --- one job ---------------------------------------------------------------------------------------------

class DuoBudget:
    """Agent runs of the duo per match (a turn or a retry is one run, tool round trips included): a cap of its own, apart from the Writer's. A job reserves the runs of its turns when it is submitted, so a job waiting behind others never finds them spent; a retry takes a run nobody reserved."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self._used: Counter = Counter()
        self._held: dict[tuple, int] = {}  # (match id, card id, version) -> runs reserved and not taken yet
        self._lock = threading.Lock()

    def _free(self, match_id: str) -> int:
        return self.cap - self._used[match_id] - sum(n for (m, *_), n in self._held.items() if m == match_id)

    def reserve(self, match_id: str, job: tuple, runs: int) -> bool:
        """Holds `runs` runs for a job (card id, version), or refuses when fewer are free."""
        with self._lock:
            if self._free(match_id) < runs:
                return False
            self._held[(match_id, *job)] = self._held.get((match_id, *job), 0) + runs
            return True

    def take(self, match_id: str, job: tuple | None = None, retry: bool = False) -> bool:
        """Takes one run: a turn from its job's reservation, a retry (or a job with none) from the free runs; refuses when there is none."""
        with self._lock:
            key = (match_id, *job) if job else None
            if not retry and self._held.get(key, 0) > 0:
                self._held[key] -= 1
            elif self._free(match_id) <= 0:
                return False
            self._used[match_id] += 1
            return True

    def release(self, match_id: str, job: tuple) -> None:
        """Gives back what a job did not take: it ended, failed or was cancelled."""
        with self._lock:
            self._held.pop((match_id, *job), None)

    def used(self, match_id: str) -> int:
        with self._lock:
            return self._used[match_id]

    def left(self, match_id: str) -> int:
        """The runs neither taken nor reserved."""
        with self._lock:
            return max(self._free(match_id), 0)


class DuoJob:
    """The state of one exchange, shared by the two TurnGates: the running order, the slot, the lines accepted, and the ledger of tool facts the lines may claim (common to both agents of the job, never to another job)."""

    def __init__(self, spec: DuoSpec, budget: DuoBudget, model_name: str) -> None:
        self.spec, self.budget, self.model_name = spec, budget, model_name
        self.plan = list(spec.plan)
        self.facts = dict(spec.facts)
        self.ledger: dict = {}
        self.meanings: dict = {}
        self.provenance: dict = {}  # field -> {tool, args, slot}
        self.subjects: dict = {}  # field -> clubs or players the fact is about
        self.conflicts: list[dict] = []
        self.lines: list[dict] = []
        self.slot = 0
        self.retries_left = DUO_RETRIES
        self.runs = 0
        self.turns: list[dict] = []
        self.failed: str | None = None
        self.started = time.perf_counter()

    def next_speaker(self, state) -> str:
        """The GroupChat selection function: the running order, no model call."""
        return self.plan[self.slot][0]

    def finished(self, conversation) -> bool:
        """The GroupChat termination condition."""
        return self.failed is not None or self.slot >= len(self.plan)

    def take_run(self, retry: bool = False) -> bool:
        if not self.budget.take(self.spec.match_id, (self.spec.card_id, self.spec.version), retry):
            return False
        self.runs += 1
        return True

    def absorb(self, response, slot: int) -> list[dict]:
        """Reads the tool calls of an agent run into the ledger: each result's facts with their meaning, tool, arguments and subject. A value that differs from a fact of the card or of the ledger is set aside as a conflict; a field of the card is never replaced."""
        calls, entries = {}, []
        messages = list(getattr(response, "messages", None) or [])
        for m in messages:
            for c in m.contents:
                if c.type == "function_call":
                    calls[c.call_id] = (c.name, c.parse_arguments() or {})
        for m in messages:
            for c in m.contents:
                if c.type != "function_result":
                    continue
                name, args = calls.get(c.call_id, ("?", {}))
                text = c.result if isinstance(c.result, str) else "".join(getattr(x, "text", "") or "" for x in (c.result or []))
                entry = {"name": name, "args": args, "fields": [], "error": None}
                try:
                    payload = json.loads(text)
                except (TypeError, ValueError):
                    payload = None
                if not isinstance(payload, dict) or not isinstance(payload.get("facts"), dict):
                    entry["error"] = str(payload["error"]) if isinstance(payload, dict) and payload.get("error") else (text or "")[:120]
                else:
                    meanings = payload.get("field_meanings") if isinstance(payload.get("field_meanings"), dict) else {}
                    subject = [s for s in payload.get("subject") or [] if isinstance(s, str)]
                    for k, v in payload["facts"].items():
                        if not scalar(v):
                            continue
                        known = self.facts[k] if k in self.facts else self.ledger.get(k, v)
                        if known != v:
                            self.conflicts.append({"field": k, "value": v, "kept": known, "tool": name})
                            continue
                        if k in self.facts:
                            continue
                        self.ledger.setdefault(k, v)
                        self.meanings.setdefault(k, meanings.get(k, ""))
                        self.provenance.setdefault(k, {"tool": name, "args": args, "slot": slot})
                        if not k.startswith("history_"):  # the span of the history is about no one
                            self.subjects.setdefault(k, subject)
                        entry["fields"].append(k)
                entries.append(entry)
        return entries

    def check(self, slot: int, text: str) -> tuple[list[dict] | None, list[str]]:
        """A turn's reply, checked: (the lines in the order of the languages, []) or (None, the problems)."""
        obj = None
        try:
            obj = json.loads((text or "").strip())
        except ValueError:
            found = re.search(r"\{.*\}", text or "", re.S)
            if found:
                try:
                    obj = json.loads(found.group(0))
                except ValueError:
                    obj = None
        if not isinstance(obj, dict) or not isinstance(obj.get("lines"), list):
            return None, ['format: the reply was not the JSON object {"lines": [...]}']
        speaker, role = self.plan[slot]
        lines = [{"speaker": speaker, "lang": l.get("lang"), "line": l.get("line"), "claims": l.get("claims") if isinstance(l.get("claims"), list) else []}
                 for l in obj["lines"] if isinstance(l, dict)]
        languages = list(self.spec.languages)
        if sorted(str(l["lang"]) for l in lines) != sorted(languages):
            return None, [f"format: one line per language: {', '.join(languages)}"]
        lines.sort(key=lambda l: languages.index(l["lang"]))
        facts = self.facts | self.ledger
        problems = [p for l in lines for p in line_problems(l, facts, role, self.subjects)]
        return (None, problems) if problems else (lines, [])

    def result(self) -> DuoResult:
        """The job's outcome: the lines when every turn passed and the whole exchange passes the final check, else the reason, and the trace either way."""
        spec = self.spec
        ok = self.failed is None and self.slot == len(self.plan)
        reason = self.failed or (None if ok else "final")
        tool_facts = {k: {"value": self.ledger[k], "tool": self.provenance[k]["tool"], "args": self.provenance[k]["args"]} for k in relied_on(self.lines, self.ledger)}
        if ok and check_duo(self.lines, spec.languages, self.facts | {k: v["value"] for k, v in tool_facts.items()}, self.plan):  # as the desk checks it again
            ok, reason = False, "final"
        trace = {"model": self.model_name, "plan": [[s, r] for s, r in self.plan], "turns": self.turns, "runs": self.runs,
                 "ms": round(1000 * (time.perf_counter() - self.started)), "tool_facts": tool_facts if ok else {}, "conflicts": self.conflicts}
        return DuoResult(spec.card_id, spec.version, spec.match_id, ok, None if ok else reason, list(self.lines) if ok else [], tool_facts if ok else {}, trace)


class TurnGate(AgentMiddleware):
    """The Verifier at each turn of the duo: a line enters the conversation only when it passes; one retry told what was rejected, then the job stops."""

    def __init__(self, job: DuoJob, speaker: str) -> None:
        self.job, self.speaker = job, speaker

    async def process(self, context, call_next) -> None:
        if context.stream:
            raise RuntimeError("the duo booth runs without streaming")
        job = self.job
        slot = job.slot
        speaker, role = job.plan[slot]
        started = time.perf_counter()
        entry = {"slot": slot, "speaker": self.speaker, "role": role, "attempts": 0, "ms": 0, "tools": [], "rejected": [], "verified": False}
        text = WITHHELD
        if speaker != self.speaker:
            job.failed = "format"
            entry["rejected"].append(f"format: {self.speaker} spoke in the slot of {speaker}")
        elif not job.take_run():
            job.failed = "budget"  # the reply is withheld before any model call
        else:
            other = HEART if self.speaker == EYE else EYE
            context.messages = [Message(role="user", contents=[f"{PERSONAS[other].short} said: {m.text}"]) if m.role == "assistant" and m.author_name == other else m
                                for m in context.messages]
            context.messages.append(Message(role="user", contents=[turn_prompt(job.spec, slot)]))
            entry["attempts"] = 1
            await call_next()
            entry["tools"] += job.absorb(context.result, slot)
            lines, problems = job.check(slot, getattr(context.result, "text", ""))
            if lines is None and job.retries_left > 0 and job.take_run(retry=True):
                job.retries_left -= 1
                entry["rejected"] += problems
                context.messages = [Message(role="user", contents=[retry_note(problems)])]  # the agent's session carries the rest of the exchange
                entry["attempts"] = 2
                await call_next()
                entry["tools"] += job.absorb(context.result, slot)
                lines, problems = job.check(slot, getattr(context.result, "text", ""))
            if lines is None:
                entry["rejected"] += problems
                job.failed = reason_of(problems)
            else:
                job.lines += lines
                job.slot += 1
                entry["verified"] = True
                text = json.dumps({"lines": [{"lang": l["lang"], "line": l["line"], "claims": l["claims"]} for l in lines]}, ensure_ascii=False)
        entry["ms"] = round(1000 * (time.perf_counter() - started))
        job.turns.append(entry)
        context.result = AgentResponse(messages=[Message(role="assistant", contents=[text], author_name=self.speaker)])  # the partner never hears a rejected draft


async def run_duo(spec: DuoSpec, model, server, budget: DuoBudget) -> DuoResult:
    """One exchange over an in-process connection to the desk's MCP server: Wren and Joss in a GroupChat whose running order is the spec's plan. Exceptions are not caught here."""
    job = DuoJob(spec, budget, model.name)
    async with Client(server, read_timeout_seconds=5) as mcp:
        available = {t.name for t in (await mcp.list_tools()).tools}
        async with contextlib.AsyncExitStack() as stack:
            agents = []
            for speaker in (HEART, EYE):
                names = [t for t in spec.tools.get(speaker, ()) if t in available]
                tools = [await stack.enter_async_context(MCPStdioTool(f"desk-{speaker}", mcp_client=mcp, allowed_tools=names, load_prompts=False,
                                                                      approval_mode="never_require"))] if names else []
                p = PERSONAS[speaker]
                agents.append(Agent(client=model.client(speaker, spec), name=speaker, description=f"{p.name}, {p.role}",
                                    instructions=persona_instructions(speaker, list(spec.languages), names), tools=tools, default_options=model.options(),
                                    middleware=[TurnGate(job, speaker)]))
            await duo_workflow(agents, job).run(briefing(spec))
    return job.result()


def duo_workflow(agents: list, job: DuoJob):
    """The GroupChat of an exchange: the job gives the floor in its running order and ends it; no model call orchestrates."""
    return GroupChatBuilder(participants=agents, selection_func=job.next_speaker, termination_condition=job.finished, max_rounds=len(job.plan) + 1, name="duo").build()


# --- who speaks --------------------------------------------------------------------------------------------

class DuoModel:
    """Who speaks for Wren and Joss: the Foundry deployment (one chat client for the duo's loop, created there at the first job) or, with dry, the offline DryChatClient."""

    def __init__(self, dry: bool = False, model: str | None = None, effort: str | None = None) -> None:
        self.dry = dry
        self.name = "dry" if dry else (model or os.environ.get("FOUNDRY_DUO_MODEL") or os.environ["FOUNDRY_MODEL"])
        self.effort = effort or os.environ.get("FOUNDRY_DUO_REASONING_EFFORT") or "low"
        self._client = None
        if not dry and not model and not os.environ.get("FOUNDRY_DUO_MODEL"):
            log.warning("the duo shares the Writer's deployment %s and its quota: FOUNDRY_DUO_MODEL gives it one of its own", self.name)

    def client(self, speaker: str, spec: DuoSpec):
        """The chat client of one agent. A Foundry client must not be shared across threads or event loops: this one is created and used on the duo's loop only."""
        if self.dry:
            return DryChatClient(spec, speaker)
        if self._client is None:
            from worker.mcp_writer import foundry_client

            self._client = foundry_client(self.name, DUO_LIMITS)
        return self._client

    def options(self) -> dict:
        return {} if self.dry else {"max_tokens": DUO_MAX_TOKENS, "reasoning": {"effort": self.effort}}

    async def aclose(self) -> None:
        """Closes the Foundry client, on the duo's loop where it was created: its OpenAI client and its project client hold the HTTP sessions."""
        client, self._client = self._client, None
        for part in (getattr(client, "client", None), getattr(client, "project_client", None)):
            close = getattr(part, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception as error:
                log.warning("duo client not closed: %r", error)


def lines_said(messages) -> set[str]:
    """The lines already in a conversation: the {"lines": [...]} objects of its messages, "Wren said: ..." included."""
    said = set()
    for m in messages:
        text = m.text or ""
        start = text.find("{")
        if start < 0:
            continue
        try:
            obj = json.JSONDecoder().raw_decode(text[start:])[0]
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("lines"), list):
            said |= {l.get("line") for l in obj["lines"] if isinstance(l, dict)}
    return said


def turn_of(messages) -> dict:
    """The turn the desk asked for last: the JSON after "Your turn: " in the latest user message that has one."""
    for m in reversed(messages):
        text = m.text or ""
        if m.role == "user" and TURN_MARK in text:
            try:
                return json.JSONDecoder().raw_decode(text[text.index(TURN_MARK) + len(TURN_MARK):])[0]
            except ValueError:
                continue
    return {}


class DryChatClient(FunctionInvocationLayer, BaseChatClient):
    """An offline stand-in for the duo model (--duo-dry): it speaks the card's template lines slot by slot, and Joss looks the player up once through MCP. No network, no cost."""

    def __init__(self, spec: DuoSpec, speaker: str) -> None:
        super().__init__(function_invocation_configuration=DUO_LIMITS)
        self.spec, self.speaker = spec, speaker
        self.looked_up = False

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        return self._reply(list(messages), dict(options))

    async def _reply(self, messages, options) -> ChatResponse:
        tools = [getattr(t, "name", "") for t in options.get("tools") or []]
        if self.speaker == EYE and not self.looked_up and self.spec.player and "player_history" in tools and options.get("tool_choice") != "none":
            self.looked_up = True
            call = Content.from_function_call(call_id="dry-1", name="player_history", arguments={"name": self.spec.player})
            return ChatResponse(messages=[Message(role="assistant", contents=[call])])
        speaker = turn_of(messages).get("speaker", self.speaker)
        said = lines_said(messages)
        out = []
        for lang in self.spec.languages:
            pool = [l for l in self.spec.template if l.get("lang") == lang]
            fresh = [l for l in pool if l.get("line") not in said]
            pick = next((l for l in fresh if l.get("speaker") == speaker), None) or (fresh[0] if fresh else pool[-1] if pool else {"line": "", "claims": []})
            out.append({"lang": lang, "line": pick.get("line", ""), "claims": copy.deepcopy(pick.get("claims") or [])})
        return ChatResponse(messages=[Message(role="assistant", contents=[json.dumps({"lines": out}, ensure_ascii=False)])])


# --- the runner -------------------------------------------------------------------------------------------

def failed_result(spec: DuoSpec, model_name: str, reason: str) -> DuoResult:
    """The result of a job that gave no lines: cancelled, stale, timed out or failed."""
    trace = {"model": model_name, "plan": [[s, r] for s, r in spec.plan], "turns": [], "runs": 0, "ms": 0, "tool_facts": {}, "conflicts": []}
    return DuoResult(spec.card_id, spec.version, spec.match_id, False, reason, [], {}, trace)


class DuoRunner:
    """Runs the duo jobs on a thread and an event loop of their own, one at a time in submission order, so the desk never waits for them."""

    def __init__(self, model, registry, history_facts=None, *, deadline_s: float = 30.0, stale_after_s: float | None = None, max_calls_per_match: int = 36) -> None:
        self.model = model
        self.registry = registry
        self.history_facts = history_facts
        self.deadline_s = deadline_s  # from the start of a job, not from its submission
        self.stale_after_s = stale_after_s  # live: a job that waited longer than this is dropped
        self.budget = DuoBudget(max_calls_per_match)
        self._results: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._futures: list[concurrent.futures.Future] = []
        self._tasks: dict[tuple[str, int], asyncio.Task] = {}
        self._submitted: dict[str, set[int]] = {}
        self._cancelled: set[tuple[str, int]] = set()
        self._closed = False
        self._server = None  # built on the duo's loop at the first job
        self._gate: asyncio.Semaphore | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="duo-agents", daemon=True)
        self._thread.start()

    def submit(self, spec: DuoSpec) -> None:
        """Queues a job; it runs after the jobs submitted before it. The runs it reserved (DuoBudget.reserve) are given back when it ends, whatever the end."""
        with self._lock:
            if self._closed:
                log.warning("duo runner closed: %s v%s not run", spec.card_id, spec.version)
                self.budget.release(spec.match_id, (spec.card_id, spec.version))
                return
            self._submitted.setdefault(spec.card_id, set()).add(spec.version)
            self._futures.append(asyncio.run_coroutine_threadsafe(self._run(spec), self._loop))

    async def _run(self, spec: DuoSpec) -> None:
        if self._gate is None:
            self._gate = asyncio.Semaphore(1)
        async with self._gate:
            try:
                result = await self._job(spec)
            finally:
                self.budget.release(spec.match_id, (spec.card_id, spec.version))  # before the next job starts
            self._results.put(result)
            log.info("duo %s v%s: %s", spec.card_id, spec.version, "verified" if result.ok else result.reason)

    async def _job(self, spec: DuoSpec) -> DuoResult:
        key = (spec.card_id, spec.version)
        result, reason, task = None, None, None
        stale = self.stale_after_s is not None and time.monotonic() - spec.submitted_at > self.stale_after_s
        if self._server is None and not stale:
            try:
                self._server = build_server(self.registry, self.history_facts)
            except Exception as error:
                reason = f"error:{type(error).__name__}"
                log.warning("duo server not built: %r", error)
        with self._lock:
            if reason is None:
                reason = "cancelled" if key in self._cancelled else "stale" if stale else None
            if reason is None:
                task = asyncio.ensure_future(run_duo(spec, self.model, self._server, self.budget))
                self._tasks[key] = task
        if task is not None:
            try:
                result = await asyncio.wait_for(task, self.deadline_s)
            except TimeoutError:
                reason = "timeout"
            except asyncio.CancelledError:
                reason = "cancelled"
            except Exception as error:
                reason = f"error:{type(leaf_error(error)).__name__}"
                log.warning("duo %s v%s failed: %r", spec.card_id, spec.version, leaf_error(error))
            finally:
                with self._lock:
                    self._tasks.pop(key, None)
        return result if result is not None else failed_result(spec, self.model.name, reason)

    def cancel(self, card_id: str) -> None:
        """Cancels every version of a card submitted so far: a queued job will not start, a running one is stopped."""
        with self._lock:
            self._cancelled |= {(card_id, v) for v in self._submitted.get(card_id, ())}
            tasks = [t for (c, _), t in self._tasks.items() if c == card_id]
        if tasks and not self._loop.is_closed():
            for task in tasks:
                self._loop.call_soon_threadsafe(task.cancel)

    def results(self) -> list[DuoResult]:
        """The results finished since the last call, without waiting."""
        out = []
        while True:
            try:
                out.append(self._results.get_nowait())
            except queue.Empty:
                return out

    def wait(self, timeout: float | None = None) -> bool:
        """Waits for every job submitted so far; True when none is left."""
        with self._lock:
            futures = list(self._futures)
        if not futures:
            return True
        _, pending = concurrent.futures.wait(futures, timeout)
        return not pending

    def close(self, timeout: float | None = None) -> None:
        """Waits for the jobs (`timeout` seconds at most), cancels what is left, closes the model's client, then stops the thread. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            cards = list(self._submitted)
        if not self.wait(timeout):
            for card_id in cards:
                self.cancel(card_id)
            self.wait(5)
        aclose = getattr(self.model, "aclose", None)
        if aclose is not None and self._thread.is_alive():
            try:
                asyncio.run_coroutine_threadsafe(aclose(), self._loop).result(5)
            except Exception as error:
                log.warning("duo model not closed: %r", error)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        if not self._thread.is_alive():
            self._loop.close()
