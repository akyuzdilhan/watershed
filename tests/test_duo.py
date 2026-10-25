"""The commentary duo, anti-repetition across a match, the product decisions on points and goals with no stake, and the insight path. No model call: crafted replies and templates only."""

import asyncio
import functools
import json
import re

import pytest

from generator import generate_match
from worker import insight_texts
from worker.desk import Desk
from worker.duo import CONDITIONS, EYE, HEART, LINES, PERSONAS, SMALL_MARGIN_PTS, TILT_PTS, PhraseMemory, fill, persona_brief
from worker.pipeline import MemorySink, Pipeline
from worker.writer import (CARD_KIND_WORDS, EXAMPLE_DUO, EXAMPLE_FACTS, INSIGHT_TEMPLATES, LOW_STAKE_TEMPLATES, OFF_AIR_FIELDS, TEMPLATES, WRITER_INSTRUCTIONS,
                           TemplateWriter, card_facts, draft, insight_facts, low_stake, ruling_facts, style_issues, template_values, verify, verify_duo, verify_reply,
                           writer_request)

LANGS = ["en", "fr", "es"]
TEAMS = {"home": "Rovers", "away": "United"}


@functools.lru_cache(maxsize=1)
def replay_names() -> dict[str, list[str]]:
    """Player names of the demo replay by side (starting eleven, then bench): read, never written in a test, since the generator may rename players."""
    info = next(m for m in generate_match(7, "demo")[0] if m["type"] == "match_info")
    return {side: [p["name"] for p in t["starting"] + t["bench"]] for side, t in info["teams"].items()}


def make_card(moment="goal", side="home", facts=None, score=None, minute="52'", **extra):
    base = {"goal": {"team_wp_before_pct": 30, "team_wp_after_pct": 54}, "save": {"xg_pct": 20, "team_wp_before_pct": 81, "team_wp_if_goal_pct": 24},
            "red_card": {"team_wp_before_pct": 55, "team_wp_after_pct": 40}, "chance": {"xg_pct": 40, "team_wp_before_pct": 40, "team_wp_if_goal_pct": 70}}
    return {"id": f"c-{moment}-{side}", "version": 1, "match_id": "m", "team": side, "team_name": TEAMS[side], "moment": moment, "minute_label": minute, "clock_s": 3000.0,
            "player": {"id": f"{side}-9", "name": "Elio Ingleby"}, "score_before": score or {"home": 1, "away": 1}, "facts": facts or base[moment], "value_pts": 24, **extra}


@pytest.fixture(scope="module")
def demo():
    stream, truth = generate_match(7, "demo")
    sink = MemorySink()
    desk = Desk(sink, TemplateWriter(), auto_approve=True)
    pipeline = Pipeline(sink=sink, desk=desk)
    for m in stream:
        pipeline.handle(m)
    pipeline.flush()
    return sink, desk


def at(sink, minute):
    return next(c for c in sink.cards.values() if c["minute_label"] == minute)


# --- the personas ---------------------------------------------------------------------------------

def test_two_complementary_personas_complete_in_three_languages():
    assert set(PERSONAS) == {HEART, EYE}
    for p in PERSONAS.values():
        for name in ("name", "short", "role", "background", "rhythm", "interaction"):
            assert getattr(p, name).strip(), (p.id, name)
        assert p.notices and p.avoids
        assert set(p.voices) == set(LANGS)
        for lang, v in p.voices.items():
            assert len(v.vocabulary) >= 4 and len(v.tics) >= 2 and v.sample.strip(), (p.id, lang)
    assert "emotion" in PERSONAS[HEART].role or "heart" in PERSONAS[HEART].role
    assert "detail" in PERSONAS[EYE].role
    tics = [t for p in PERSONAS.values() for v in p.voices.values() for t in v.tics]
    assert len(tics) == len(set(tics))
    brief = persona_brief(LANGS)
    assert all(p.name in brief for p in PERSONAS.values()) and all(t in brief for t in tics)
    assert brief in WRITER_INSTRUCTIONS


def test_line_templates_cover_every_moment_in_three_languages():
    for kind, by_lang in LINES.items():
        assert set(by_lang) == set(LANGS), kind
        shapes = {lang: sorted(v) for lang, v in by_lang.items()}
        assert len({tuple(s) for s in shapes.values()}) == 1, (kind, shapes)


def all_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from all_strings(v)
    else:
        for v in obj:
            yield from all_strings(v)


CASES = [  # (line kind, card, extra facts)
    ("goal", make_card("goal", scorer_match_goals=4, minute="83'"), {}),
    ("goal_low", make_card("goal", facts={"team_wp_before_pct": 99, "team_wp_after_pct": 99}, score={"home": 4, "away": 2}, minute="88'", scorer_match_goals=4), {}),
    ("save", make_card("save"), {}),
    ("red_card", make_card("red_card", "away", card_kind="second_yellow"), {}),
    ("red_card", make_card("red_card", "home", card_kind="red"), {}),
    ("red_card", make_card("red_card", "away"), {}),
    ("chance", make_card("chance", "away"), {}),
    ("ruling", make_card("goal", ruled_out={"reason": "offside", "margin_cm": 25}), {}),
]


def lines_for(kind, lang, facts):
    """Every line of a kind, a red card's heart lines for its own card kind only (a second yellow is never told as a straight red)."""
    lines = LINES[kind][lang]
    if kind == "red_card":
        lines = dict(lines, heart=[lines["heart"].get(facts.get("card_kind"), []), lines["heart"]["any"]])
    return all_strings(lines)


@pytest.mark.parametrize("kind,card,extra", CASES, ids=[f"{c[0]}-{c[1].get('card_kind', '')}" for c in CASES])
def test_every_duo_line_passes_the_verifier_with_its_numbers_claimed(kind, card, extra):
    facts, _ = ruling_facts(card, TEAMS) if kind == "ruling" else card_facts(card, TEAMS)
    facts |= extra
    for lang in LANGS:
        values, fields = template_values(card, facts, lang)
        values |= {"subject": "Rovers"} | {c: "" for c in CONDITIONS}  # every line, whatever its condition
        for template in lines_for(kind, lang, facts):
            filled = fill(template, values, fields)
            assert filled, (lang, template)
            line = {"speaker": HEART, "lang": lang, "line": filled[0], "claims": filled[1]}
            report = verify_duo([line, dict(line, speaker=EYE)], [lang], facts)
            entry = report["lines"][0]
            warnings = [w for w in entry["style_warnings"] if w["rule"] != "count_word_unbacked"]  # a story line is only picked when its count matches
            assert entry["verified"] and not warnings and not entry["unknown_names"], (lang, filled[0], entry)


def test_insight_lines_and_texts_pass_the_verifier():
    doc = {"id": "insight-m-sub4", "type": "insight", "match_id": "m", "kind": "record", "headline_facts": {"player": "Dorian Lightfoot", "goals": 4}, "field_meanings": {}}
    facts, _ = insight_facts(doc, TEAMS)
    for lang in LANGS:
        for template in all_strings(LINES["insight"][lang]) :
            filled = fill(template, {"subject": "Dorian Lightfoot"}, {})
            assert verify_duo([{"speaker": HEART, "lang": lang, "line": filled[0], "claims": []}, {"speaker": EYE, "lang": lang, "line": "Yes.", "claims": []}], [lang], facts)["verified"], filled
        for kind, pairs in INSIGHT_TEMPLATES[lang].items():
            for headline, why in pairs:
                values = {"subject": "Dorian Lightfoot", "home_team": "Rovers", "away_team": "United"}
                text = {"headline": fill(headline, values, {})[0], "why": fill(why, values, {})[0], "lang": lang, "claims": []}
                _, report = verify(json.dumps([text], ensure_ascii=False), [lang], facts)
                assert report["verified"] and not report["cards"][0]["style_warnings"], (lang, kind, text, report)


@pytest.mark.parametrize("side", ["home", "away"])
def test_every_template_variant_passes_every_check(side):
    for moment, by_lang in TEMPLATES.items():
        card = make_card(moment, side, scorer_match_goals=2)
        facts, _ = card_facts(card, TEAMS)
        for lang, t in by_lang.items():
            values, fields = template_values(card, facts, lang)
            heads = t["headlines"] + [h for pool in t.get("situations", {}).values() for h in pool]
            for headline in heads:
                for why in t["whys"]:
                    h, w = fill(headline, values, fields), fill(why, values, fields)
                    text = {"headline": h[0], "why": w[0], "lang": lang, "claims": h[1] + w[1]}
                    _, report = verify(json.dumps([text], ensure_ascii=False), [lang], facts)
                    e = report["cards"][0]
                    assert report["verified"] and not e["style_warnings"] and not e["unknown_names"], (moment, lang, text, e)
                    assert TEAMS[side] in w[0]
                    if moment == "goal":
                        assert "Elio Ingleby" in h[0]


def test_low_stake_templates_have_no_probability():
    card = make_card("goal", facts={"team_wp_before_pct": 98, "team_wp_after_pct": 99}, score={"home": 4, "away": 2}, minute="88'", scorer_match_goals=4)
    facts, _ = card_facts(card, TEAMS)
    for lang, t in LOW_STAKE_TEMPLATES.items():
        values, fields = template_values(card, facts, lang)
        for headline in t["headlines"]:
            for why in t["whys"]:
                h, w = fill(headline, values, fields), fill(why, values, fields)
                text = {"headline": h[0], "why": w[0], "lang": lang, "claims": h[1] + w[1]}
                _, report = verify(json.dumps([text], ensure_ascii=False), [lang], facts)
                assert report["verified"] and not report["cards"][0]["style_warnings"], (lang, text, report)
                assert "%" not in h[0] + w[0] and not re.search(r"\d", h[0] + w[0])


def test_instruction_duo_example_passes_the_verifier():
    report = verify_duo(EXAMPLE_DUO, LANGS, EXAMPLE_FACTS)
    assert report["verified"] and all(not e["style_errors"] for e in report["lines"])
    assert json.dumps(EXAMPLE_DUO, ensure_ascii=False) in WRITER_INSTRUCTIONS


def test_duo_verifier_rejects_unclaimed_numbers_one_speaker_and_too_many_lines():
    card = make_card()
    facts, _ = card_facts(card, TEAMS)
    good = [{"speaker": HEART, "lang": "en", "line": "Ingleby! Rovers are level!", "claims": []},
            {"speaker": EYE, "lang": "en", "line": "Rovers go from 30% to 54% to win.", "claims": [{"value": 30, "source_field": "rovers_wp_before_pct"}, {"value": 54, "source_field": "rovers_wp_after_pct"}]}]
    assert verify_duo(good, ["en"], facts)["verified"]
    unclaimed = [good[0], dict(good[1], line="Rovers go from 30% to 61% to win.")]
    assert not verify_duo(unclaimed, ["en"], facts)["verified"]
    assert not verify_duo([good[0], dict(good[0])], ["en"], facts)["verified"]  # the same speaker twice in a row
    assert not verify_duo(good * 3, ["en"], facts)["verified"]  # six lines
    assert not verify_duo([dict(good[1], line="Ils passent de 30 % à 54 %.", lang="fr"), dict(good[0], lang="fr")], ["fr"], facts)["verified"]  # a probability without its team
    assert not verify_duo([good[0]], ["en"], facts)["verified"]


# --- the demo match ----------------------------------------------------------------------------------

def test_every_demo_card_has_a_verified_duo(demo):
    sink, _ = demo
    for card in sink.cards.values():
        facts, _ = card_facts(card, TEAMS)
        report = verify_duo(card["duo"], LANGS, facts)
        assert report["verified"], (card["minute_label"], report)
        for lang in LANGS:
            lines = [l for l in card["duo"] if l["lang"] == lang]
            assert 2 <= len(lines) <= 4 and lines[0]["speaker"] in PERSONAS
        assert card["duo_writer"] == "template"
        numbers = [l for l in card["duo"] if re.search(r"\d", l["line"])]
        assert all(l["claims"] for l in numbers)


def test_no_identical_headline_and_varied_openings_across_the_demo(demo):
    sink, desk = demo
    cards = sorted(sink.cards.values(), key=lambda c: c["clock_s"])
    memory = desk.memory("demo-0007")
    for lang in LANGS:
        headlines = [t["headline"] for c in cards for t in c["texts"] if t["lang"] == lang]
        assert len(headlines) == len(set(headlines)) == len(cards), headlines
        lines = [t["why"] for c in cards for t in c["texts"] if t["lang"] == lang] + [l["line"] for c in cards for l in c["duo"] + c.get("duo_ruling", []) if l["lang"] == lang]
        openings = [memory.opening(x) for x in lines]
        ratio = len(set(openings)) / len(openings)
        assert ratio >= 0.8, (lang, ratio, sorted(openings))
        said = [PhraseMemory.key(x) for x in lines]
        assert len(said) - len(set(said)) <= 1, (lang, [x for x in said if said.count(x) > 1])
        tics = [t for p in PERSONAS.values() for t in p.voices[lang].tics]
        for tic in tics:
            assert sum(tic in x for x in lines) <= 1, (lang, tic)


def test_the_scorer_story_is_counted_without_goals_ruled_out(demo):
    sink, _ = demo
    assert [at(sink, m).get("scorer_match_goals") for m in ("52'", "57'", "66'", "83'")] == [1, 2, 3, 4]
    en = [l["line"] for l in at(sink, "83'")["duo"] if l["lang"] == "en"]
    assert any("fourth" in l for l in en) and not any("goal number" in l for l in en), en


def test_var_ruling_is_recorded_with_its_reason_and_margin(demo):
    sink, _ = demo
    offside, handball = at(sink, "63'"), at(sink, "69'")
    assert offside["ruled_out"] == {"reason": "offside", "cause": "var_offside", "minute_label": "64'", "margin_cm": 25}
    assert handball["ruled_out"]["reason"] == "handball" and handball["ruled_out"]["margin_cm"] is None
    for card in (offside, handball):
        facts, _ = ruling_facts(card, TEAMS)
        assert verify_duo(card["duo_ruling"], LANGS, facts)["verified"]
    assert any("whisker" in l["line"] for l in offside["duo_ruling"] if l["lang"] == "en")  # 25 cm is a tight call
    assert not any(w in l["line"] for l in offside["duo_ruling"] for w in ("25", "cm", "centim"))  # the margin never goes on air
    assert all("VAR" in " ".join(l["line"] for l in handball["duo_ruling"] if l["lang"] == lang) or lang != "en" for lang in LANGS)


# --- product decisions ---------------------------------------------------------------------------------

def test_points_on_screen_are_only_a_change_in_win_probability(demo):
    sink, _ = demo
    for card in sink.cards.values():
        facts, _ = card_facts(card, TEAMS)
        assert "value_pts" not in facts
        allowed = {facts[k] for k in ("swing_pts", "save_value_pts") if k in facts}
        for x in [t["headline"] + " " + t["why"] for t in card["texts"]] + [l["line"] for l in card["duo"]]:
            for m in re.finditer(r"(\d+)\s+(?:points?|puntos?)", x.replace("\xa0", " ")):
                assert int(m.group(1)) in allowed, (card["minute_label"], x)
    save = at(sink, "72'")
    assert save["value_pts"] == 12 and not any("12 " in t["why"] for t in save["texts"])


def test_a_goal_with_no_stake_is_a_milestone_with_low_priority(demo):
    sink, _ = demo
    late = at(sink, "88'")
    assert late["on_air_priority"] == "low" and low_stake(late)
    assert all(c["on_air_priority"] == "normal" for c in sink.cards.values() if c is not late)
    facts, _ = card_facts(late, TEAMS)
    assert facts["stake"] == "low" and not [k for k in facts if "_wp_" in k or k.endswith("_pts")]
    for x in [t["headline"] + " " + t["why"] for t in late["texts"]] + [l["line"] for l in late["duo"]]:
        assert "%" not in x and not re.search(r"\b(?:points?|puntos?)\b", x), x
    assert late["verification"]["verified"]


def test_a_stakeless_goal_from_4_2_to_5_2_at_88():
    card = make_card("goal", facts={"team_wp_before_pct": 98, "team_wp_after_pct": 99}, score={"home": 4, "away": 2}, minute="88'", scorer_match_goals=4)
    assert low_stake(card) and not low_stake(make_card("goal", facts={"team_wp_before_pct": 91, "team_wp_after_pct": 99}))
    facts, _ = card_facts(card, TEAMS)
    out = json.loads(TemplateWriter().write(card, facts, LANGS, duo=True))
    texts, lines, report = verify_reply(json.dumps(out, ensure_ascii=False), LANGS, facts, duo=True)
    assert report["verified"]
    words = " ".join(t["headline"] + " " + t["why"] for t in texts) + " " + " ".join(l["line"] for l in lines)
    assert not re.search(r"\d", words)
    assert re.search(r"\b(?:five|fifth|fourth|cinq|cinquième|quatrième|cinco|quinto|cuarto)\b", words.lower()), words
    # a model that still writes a probability for it fails: the field is not in the facts
    bad = [{"headline": "Ingleby makes it five", "why": "Rovers go from 98% to 99% to win.", "lang": "en",
            "claims": [{"value": 98, "source_field": "rovers_wp_before_pct"}, {"value": 99, "source_field": "rovers_wp_after_pct"}]}]
    assert not verify(json.dumps(bad), ["en"], facts)[1]["verified"]


# --- the Writer agent path, with crafted replies ------------------------------------------------------

class FakeWriter:
    """Replays crafted replies and records what each request carried: no model call."""

    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def write_async(self, card, facts, languages, note=None, avoid=None, duo=False):
        self.calls.append({"card": card["id"], "note": note, "avoid": avoid, "duo": duo, "request": writer_request(card, languages, avoid, duo)})
        return self.replies.pop(0)


GOAL_CLAIMS = [{"value": 30, "source_field": "rovers_wp_before_pct"}, {"value": 54, "source_field": "rovers_wp_after_pct"}, {"value": 24, "source_field": "swing_pts"}]


def reply(headline="Ingleby levels it for Rovers", duo=True):
    texts = [{"headline": headline, "why": "Rovers go from 30% to 54% to win, up 24 points.", "lang": "en", "claims": GOAL_CLAIMS}]
    lines = [{"speaker": "wren", "lang": "en", "line": "Ingleby! And this place has woken up!", "claims": []},
             {"speaker": "joss", "lang": "en", "line": "That goal is worth 24 points to Rovers.", "claims": [GOAL_CLAIMS[2]]}]
    return json.dumps({"texts": texts, "duo": lines} if duo else texts)


def test_one_call_returns_texts_and_duo():
    writer = FakeWriter([reply()])
    d = asyncio.run(draft(make_card(), TEAMS, writer, ["en"], memory=PhraseMemory(), duo=True))
    assert d.writer == "fake" and d.duo_writer == "fake" and d.attempts == 1 and d.report["verified"]
    assert [l["speaker"] for l in d.duo] == ["wren", "joss"]
    assert writer.calls[0]["duo"] and writer.calls[0]["request"]["duo"] is True


def test_the_prompt_receives_what_the_match_already_said():
    memory = PhraseMemory()
    writer = FakeWriter([reply(), reply("Ingleby again for Rovers")])
    asyncio.run(draft(make_card(), TEAMS, writer, ["en"], memory=memory, duo=True))
    second = dict(make_card(), id="c-2")
    d = asyncio.run(draft(second, TEAMS, writer, ["en"], memory=memory, duo=True))
    avoid = writer.calls[1]["avoid"]
    assert "Ingleby levels it for Rovers" in avoid["en"]["headlines"] and avoid["en"]["openings"] and "levels" in avoid["en"]["verbs"]
    assert writer.calls[1]["request"]["avoid"] == avoid
    assert d.report["verified"] and d.report["repetition"]["warnings"]  # same why and lines: warned, not failed


def test_an_identical_headline_fails_and_is_retried():
    memory = PhraseMemory()
    asyncio.run(draft(make_card(), TEAMS, FakeWriter([reply()]), ["en"], memory=memory, duo=True))
    writer = FakeWriter([reply(), reply("A second for Ingleby")])
    d = asyncio.run(draft(dict(make_card(), id="c-2"), TEAMS, writer, ["en"], memory=memory, duo=True))
    assert d.attempts == 2 and d.writer == "fake" and "already used" in writer.calls[1]["note"]
    assert d.texts[0]["headline"] == "A second for Ingleby"


def test_model_texts_are_kept_when_only_the_duo_fails():
    bad_duo = json.loads(reply())
    bad_duo["duo"] = bad_duo["duo"][:1]  # one line only
    writer = FakeWriter([json.dumps(bad_duo)] * 2)
    d = asyncio.run(draft(make_card(), TEAMS, writer, ["en"], memory=PhraseMemory(), duo=True))
    assert d.writer == "fake" and d.duo_writer == "template" and d.report["verified"] and len(d.duo) >= 2


def test_a_writer_without_duo_mode_gets_template_lines():
    class Old:
        name = "old"

        async def write_async(self, card, facts, languages, note=None):
            return reply(duo=False)

    d = asyncio.run(draft(make_card(), TEAMS, Old(), ["en"], memory=PhraseMemory(), duo=True))
    assert d.writer == "old" and d.duo_writer == "template" and d.report["verified"]


def test_verifier_warns_on_a_reused_phrase_and_fails_a_reused_headline():
    memory = PhraseMemory()
    texts = [{"headline": "Ingleby scores for Rovers", "why": "Rovers go from 30% to 54% to win, up 24 points.", "lang": "en"}]
    memory.record("a", texts, [{"speaker": "wren", "lang": "en", "line": "Hold on to your seats! Here we go again!"}], {"scorer": "Elio Ingleby", "score_before": "Rovers 0-0 United"})
    errors, warnings = memory.issues("b", texts, [{"speaker": "wren", "lang": "en", "line": "Hold on to your seats! What a game!"}])
    assert [e["rule"] for e in errors] == ["repeated_headline"]
    assert {"repeated_opening", "repeated_phrase", "repeated_tic"} <= {w["rule"] for w in warnings}
    assert memory.issues("a", texts, [])[0] == []  # a new version of the same card is not a repeat


def test_template_headlines_never_repeat_even_for_the_same_scorer():
    memory = PhraseMemory()
    seen = set()
    for i in range(14):
        card = dict(make_card(score={"home": i, "away": 20}), id=f"g{i}")
        d = asyncio.run(draft(card, TEAMS, TemplateWriter(), LANGS, memory=memory, duo=True))
        assert d.report["verified"], d.report
        for t in d.texts:
            assert (t["lang"], t["headline"]) not in seen
            seen.add((t["lang"], t["headline"]))


# --- insights and storyline through the desk -------------------------------------------------------------

INSIGHT = {"id": "insight-m-sub4", "type": "insight", "match_id": "m", "key": "sub4", "kind": "record", "minute_label": "83'", "clock_s": 5000.0, "event_id": "e1",
           "headline_facts": {"player": "Dorian Lightfoot", "goals_by_substitute": 4, "past_matches": 18620, "times_before": 0},
           "field_meanings": {"goals_by_substitute": "goals by the substitute in this match", "past_matches": "matches in the league history", "times_before": "substitutes who scored 4 before"},
           "rarity": {"p": 0.0, "surprisal_bits": 14.2, "basis": "4 goals by a substitute: 0 in 18,620 matches since 1976/77"}, "value": 90, "status": "proposed"}


def test_write_insight_with_templates_goes_through_the_desk():
    sink = MemorySink()
    desk = Desk(sink, TemplateWriter())
    doc = desk.write_insight(json.loads(json.dumps(INSIGHT)), TEAMS)
    assert doc["status"] == "pending" and doc["writer"] == "template" and doc["verification"]["verified"]
    assert sorted(t["lang"] for t in doc["texts"]) == sorted(LANGS) and doc["machine_translated"] == ["es"]
    facts, _ = insight_facts(doc, TEAMS)
    assert verify_duo(doc["duo"], LANGS, facts)["verified"]
    assert sink.insights[doc["id"]]["type"] == "insight"  # insights have their own store, apart from the cards
    assert [e["action"] for e in sink.audit if e["card_id"] == doc["id"]] == ["written"]
    desk.approve(doc, "producer-1")
    assert doc["status"] == "approved" and [e["action"] for e in sink.audit if e["card_id"] == doc["id"]][-1] == "approved"


def test_a_model_insight_with_a_count_of_matches_is_retried():
    """A count of matches is for the producer: the claimed number is right, the reply is still refused, and the retry is told why."""
    player = INSIGHT["headline_facts"]["player"]
    claims = [{"value": 4, "source_field": "goals_by_substitute"}, {"value": 0, "source_field": "times_before"}, {"value": 18620, "source_field": "past_matches"}]
    counted = [{"headline": f"{player}, a first in the league", "why": f"{player} scores 4 as a substitute: 0 times in 18,620 matches before.", "lang": "en", "claims": claims}]
    told = [{"headline": f"{player}, a first in the league", "why": f"{player} scores 4 as a substitute, a first in the league.", "lang": "en", "claims": claims[:1]}]
    lines = [{"speaker": "wren", "lang": "en", "line": "Four from the bench! Is that a first, Joss?", "claims": []},
             {"speaker": "joss", "lang": "en", "line": "It is, Wren: nobody had done it before.", "claims": []}]
    writer = FakeWriter([json.dumps({"texts": counted, "duo": lines}), json.dumps({"texts": told, "duo": lines})])
    doc = Desk(MemorySink(), writer, auto_approve=True, languages=("en",)).write_insight(json.loads(json.dumps(INSIGHT)), TEAMS)
    assert doc["writer"] == "fake" and doc["duo_writer"] == "fake" and doc["status"] == "approved" and doc["verification"]["verified"]
    assert writer.calls[0]["request"]["document"] == "insight" and "league_rate" in writer.calls[1]["note"]


def test_write_insight_falls_back_to_the_template_after_one_retry():
    invented = json.dumps({"texts": [{"headline": "Lightfoot record", "why": "Lightfoot scores 4, a first in 19,000 matches.", "lang": "en", "claims": []}], "duo": []})
    writer = FakeWriter([invented, invented])
    doc = Desk(MemorySink(), writer, languages=("en",)).write_insight(json.loads(json.dumps(INSIGHT)), TEAMS)
    assert doc["writer"] == "template" and doc["verification"] == {"verified": True, "attempts": 2} | ({"repetition_warnings": doc["verification"]["repetition_warnings"]} if "repetition_warnings" in doc["verification"] else {})
    assert "unclaimed" in writer.calls[1]["note"] or "bad_claims" in writer.calls[1]["note"]


def test_write_insight_keeps_the_documents_own_verified_texts_as_template():
    doc = json.loads(json.dumps(INSIGHT))
    doc["texts"] = [{"headline": "Four goals from the bench", "why": "Dorian Lightfoot scores 4 as a substitute.", "lang": "en", "claims": [{"value": 4, "source_field": "goals_by_substitute"}]}]
    out = Desk(MemorySink(), TemplateWriter(), languages=("en",)).write_insight(doc, TEAMS)
    assert out["texts"][0]["headline"] == "Four goals from the bench"


def test_a_documents_own_text_is_kept_only_if_it_says_nothing_off_air():
    doc = match_insights()[0]  # a brace off the bench
    player, n = doc["headline_facts"]["player"], doc["headline_facts"]["history_opportunities"]
    doc["texts"] = [{"headline": f"{player} again", "why": f"{player} scores twice off the bench, out of {n} chances.", "lang": "en",
                     "claims": [{"value": n, "source_field": "history_opportunities"}]}]
    out = Desk(MemorySink(), TemplateWriter(), languages=("en",)).write_insight(doc, TEAMS)
    assert out["texts"][0]["headline"] != f"{player} again" and out["verification"]["verified"]


def test_storyline_goes_through_the_same_path():
    story = {"id": "storyline-m", "type": "storyline", "match_id": "m", "facts": {"rovers_title_chance_pct": 12}, "field_meanings": {"rovers_title_chance_pct": "Rovers' chance of the title, in %"}}
    doc = Desk(MemorySink(), TemplateWriter(), auto_approve=True).write_insight(story, TEAMS)
    assert doc["status"] == "approved" and doc["verification"]["verified"] and len(doc["texts"]) == 3 and doc["duo"]


ENGINE_FACTS = {  # the headline facts the insight engine gives a substitute's four goals and a comeback
    "scorer-home-20-4": {"player": "Dorian Lightfoot", "team": "Rovers", "goals": 4, "goal_minutes": "52', 57', 66', 83'", "substitute": True, "came_on": "46'",
                         "history_count": 0, "history_opportunities": 19000, "history_since": "1976/77", "history_seasons": 50},
    "comeback-home-2-3": {"team": "Rovers", "opponent": "United", "deficit_goals": 2, "trailing_score": "0-2", "stage": "won", "score": "5-2", "club_last_season": "2019/20", "club_seasons_since": 7,
                          "history_count": 114, "history_opportunities": 9579, "history_since": "1976/77", "history_seasons": 50, "history_per_season": 2.3, "one_in": 83,
                          "last_season": "2025/26", "last_match": "Stellbury 4-3 Netherwick"},
}


def test_engine_insights_get_texts_with_their_numbers():
    for key, facts in ENGINE_FACTS.items():
        doc = {"id": f"insight-m-{key}", "type": "insight", "match_id": "m", "key": key, "kind": "rarity", "headline_facts": facts,
               "field_meanings": {k: k for k in facts}, "value": 80, "status": "proposed"}
        out = Desk(MemorySink(), TemplateWriter(), languages=LANGS).write_insight(doc, TEAMS)
        assert out["verification"]["verified"] and out["duo"]
        en = next(t for t in out["texts"] if t["lang"] == "en")
        assert {c["source_field"] for c in en["claims"]} & {"history_seasons", "club_seasons_since"}  # rarity told in seasons, for who did it
        whys = " ".join(t["why"] for t in out["texts"]).replace("\u202f", " ")
        assert not any(n in whys for n in ("9,579", "19,000", "9 579", "19 000", "9.579", "19.000"))  # never in matches


def test_a_starter_never_gets_the_bench_lines():
    facts = dict(ENGINE_FACTS["scorer-home-20-4"], substitute=False)
    facts.pop("came_on")
    doc = {"id": "insight-m-scorer-home-9-3", "type": "insight", "match_id": "m", "key": "scorer-home-9-3", "kind": "milestone", "headline_facts": facts,
           "field_meanings": {k: k for k in facts}, "value": 60, "status": "proposed"}
    out = Desk(MemorySink(), TemplateWriter(), languages=LANGS).write_insight(doc, TEAMS)
    text = json.dumps(out["texts"] + out["duo"], ensure_ascii=False).lower()
    assert not any(w in text for w in ("bench", "banc", "banquillo"))


# --- off air: league-wide rates, ratios, match counts and bits ------------------------------------------------

def match_insights() -> list[dict]:
    """The insights of a match like the demo, in the order the engine writes them: a substitute's brace (seen every season), hat-trick (rare) and four (never), a comeback that
    draws level, goes ahead and wins, a half-time lead lost, five goals from 1.3 xG, and the stat of the match with its ranking against the runner-up."""
    four = ENGINE_FACTS["scorer-home-20-4"]
    base = {k: four[k] for k in ("player", "team", "substitute", "came_on", "history_since", "history_seasons")}
    last = replay_names()["away"][0]
    scorer = {2: dict(base, goals=2, goal_minutes="52', 57'", history_count=150, history_opportunities=19000, history_rank=151, history_per_season=3, one_in=127,
                      surprisal_bits=7.0, last_season="2025/26", last_player=last),
              3: dict(base, goals=3, goal_minutes="52', 57', 66'", history_count=5, history_opportunities=19000, history_rank=6, one_in=3167, surprisal_bits=11.6,
                      last_season="2021/22", last_player=last),
              4: dict(four, history_rank=1, one_in=19001, surprisal_bits=14.2)}
    comeback = {stage: dict(ENGINE_FACTS["comeback-home-2-3"], stage=stage, surprisal_bits=6.4) for stage in ("level", "ahead", "won")}
    ht = {stage: {"team": "United", "half_time_score": "0-2", "stage": stage, "venue": "away", "club_last_season": "2020/21", "club_seasons_since": 6, "history_count": 42,
                  "history_opportunities": 3432, "history_since": "1976/77", "history_seasons": 50, "one_in": 82, "surprisal_bits": 6.4} for stage in ("behind", "lost")}
    stat = dict(scorer[4], top_fact="scorer-home-20-4", top_value=80, runner_up="comeback-home-2-3", runner_up_value=65, runner_up_surprisal_bits=6.4, times_rarer_than_runner_up=228)
    docs = [("scorer-home-20-2", scorer[2]), ("comeback-home-2-1", comeback["level"]), ("scorer-home-20-3", scorer[3]), ("comeback-home-2-2", comeback["ahead"]),
            ("ht-lead-lost-away-1", ht["behind"]), ("scorer-home-20-4", scorer[4]), ("comeback-home-2-3", comeback["won"]), ("ht-lead-lost-away-2", ht["lost"]),
            ("xg-ft-home-5", {"team": "Rovers", "goals": 5, "xg": 1.3, "one_in": 101, "surprisal_bits": 6.7}), ("stat-of-the-match", stat)]
    return [{"id": f"insight-m-{key}", "type": "insight", "match_id": "m", "key": key, "kind": "rarity", "headline_facts": facts, "field_meanings": {k: k for k in facts},
             "rarity": {"basis": f"{facts.get('history_count', 0)} in {facts.get('history_opportunities', 0):,} since 1976/77"}, "value": 80, "status": "proposed"}
            for key, facts in docs]


OFF_AIR_VALUES = {127, 3167, 19001, 101, 228, 2.3, 14.2, 11.6, 6.4, 6.7, 19000, 9579, 3432, 82}  # rates, counts, ratios and bits of match_insights()


def test_insight_texts_and_lines_keep_league_wide_rates_off_air():
    from worker.writer import numbers_in

    desk = Desk(MemorySink(), TemplateWriter(), languages=LANGS)
    for doc in match_insights():
        basis = doc["rarity"]["basis"]
        out = desk.write_insight(doc, TEAMS)
        assert out["verification"]["verified"] and out["rarity"]["basis"] == basis, out["id"]  # the producer keeps the numbers
        for lang in LANGS:
            said = [t[k] for t in out["texts"] if t["lang"] == lang for k in ("headline", "why")] + [l["line"] for l in out["duo"] if l["lang"] == lang]
            for text in said:
                assert "league_rate" not in {e["rule"] for e in style_issues("", text, lang, [], {})[0]}, (out["id"], text)
                assert not {v for v, _ in numbers_in(text)} & OFF_AIR_VALUES, (out["id"], text)


def test_no_insight_line_is_said_twice_in_a_match():
    desk = Desk(MemorySink(), TemplateWriter(), languages=LANGS)
    outs = [desk.write_insight(doc, TEAMS) for doc in match_insights()]
    for lang in LANGS:
        lines = [PhraseMemory.key(l["line"]) for o in outs for l in o["duo"] if l["lang"] == lang]
        assert len(lines) == len(set(lines)), (lang, [l for l in lines if lines.count(l) > 1])
    hearts = [l["line"] for o in outs if o["key"].startswith("comeback") for l in o["duo"] if l["lang"] == "en" and l["speaker"] == HEART]
    assert len(hearts) == 3 and len(set(hearts)) == 3  # one line of its own at each stage


def test_comeback_lines_are_their_own_at_each_stage():
    for lang in LANGS:
        for part in ("heart", "eye"):
            stages = [set(insight_texts.T["comeback"][lang][part][s]) for s in ("level", "ahead", "won")]
            assert all(not a & b for i, a in enumerate(stages) for b in stages[i + 1:]), (lang, part)


def every_stage() -> list[tuple[str, dict]]:
    """The facts of match_insights(), and its comebacks and lost leads again as a first for the club: between them, every template of every family is usable."""
    out = [(d["key"], d["headline_facts"]) for d in match_insights()]
    for key, facts in list(out):
        if "club_last_season" in facts:
            out.append((key, {k: v for k, v in facts.items() if k not in ("club_last_season", "club_seasons_since")} | {"club_first_time": True}))
    return out


def test_every_family_line_passes_the_verifier_at_every_stage():
    from worker.writer import check_numbers, on_air

    for key, facts in every_stage():
        fam = insight_texts.family({"key": key, "headline_facts": facts})
        full, _ = insight_facts({"headline_facts": facts}, TEAMS)
        air, _ = on_air(full, {})
        for lang in LANGS:
            t, (values, fields) = insight_texts.T[fam][lang], insight_texts.values(full, lang)
            parts = {p: insight_texts.part(t, p, full) for p in ("headlines", "whys", "heart", "eye")} | {"stat": insight_texts.STAT_HEART.get(fam, {}).get(lang, [])}
            for part, templates in parts.items():
                for template in (x for x in templates if insight_texts.usable(x, full)):
                    done = fill(insight_texts.clean(template), values, fields)
                    if done is None:  # a placeholder these facts lack: the picker skips it too
                        continue
                    nums = check_numbers(done[0], done[1], air)
                    errors, _ = style_issues(done[0], "", lang, nums["good"], air) if part == "headlines" else style_issues("", done[0], lang, nums["good"], air)
                    assert not errors and not nums["bad_claims"] and not nums["unclaimed_numbers"], (key, lang, part, done[0], errors, nums)


class FactsSpy(FakeWriter):
    """Keeps the facts the model is given."""

    async def write_async(self, card, facts, languages, note=None, avoid=None, duo=False):
        self.facts = facts[0]
        return await super().write_async(card, facts, languages, note, avoid, duo)


def test_the_model_never_gets_league_wide_rates_ratios_or_bits():
    stat = match_insights()[-1]
    rate = [{"headline": "Stat of the match", "why": "About one match in 19001 goes that way.", "lang": "en", "claims": [{"value": 19001, "source_field": "one_in"}]}]
    writer = FactsSpy([json.dumps({"texts": rate, "duo": []})] * 2)
    out = Desk(MemorySink(), writer, languages=("en",)).write_insight(stat, TEAMS)
    assert not set(writer.facts) & set(OFF_AIR_FIELDS) and {"history_seasons", "player", "goals", "top_fact"} <= set(writer.facts)
    assert out["writer"] == "template" and out["verification"]["verified"] and "bad_claims" in writer.calls[1]["note"]


# --- the duo says what is true of the moment ---------------------------------------------------------------

def filled(card, lang="en"):
    """Every duo line the templates may give this card, conditions applied."""
    facts, _ = card_facts(card, TEAMS)
    values, fields = template_values(card, facts, lang)
    kind = "goal_low" if facts.get("stake") == "low" else card["moment"]
    return [f[0] for t in all_strings(LINES[kind][lang]) if (f := fill(t, values, fields))]


ROAR = {"en": ("woken up", "enjoying the noise"), "fr": ("stade se réveille", "bruit du stade"), "es": ("estadio despierta", "rugido del estadio")}


@pytest.mark.parametrize("side", ["home", "away"])
def test_only_a_home_goal_wakes_the_stadium(side):
    level = make_card("goal", side, score={"home": 0, "away": 1} if side == "home" else {"home": 1, "away": 0})
    for lang in LANGS:
        roars = [l for l in filled(level, lang) if any(w in l for w in ROAR[lang])]
        assert bool(roars) == (side == "home"), (lang, roars)


def test_the_man_this_time_needs_a_new_scorer():
    lead = {"home": 1, "away": 0}
    assert not [l for l in filled(make_card("goal", score=lead, scorer_match_goals=2)) if "the man this time" in l]  # he scored the first one too
    assert [l for l in filled(make_card("goal", score=lead, scorer_match_goals=1)) if "the man this time" in l]


@pytest.mark.parametrize("value", [10, 14, 15, 57])
def test_small_margins_under_15_points_and_quietly_huge_above(value):
    save = make_card("save", facts={"xg_pct": 20, "team_wp_before_pct": 80, "team_wp_if_goal_pct": 80 - value})
    words = {"en": ("Small margins", "Quietly huge"), "fr": ("Petites marges", "L'air de rien"), "es": ("Pequeños márgenes", "Sin hacer ruido")}
    for lang in LANGS:
        lines = filled(save, lang)
        small, huge = (any(l.startswith(w) for l in lines) for w in words[lang])
        assert small == (value < SMALL_MARGIN_PTS) and huge == (value >= SMALL_MARGIN_PTS), (lang, value)


@pytest.mark.parametrize("after", [54, 50, 38])
def test_the_whole_match_tilts_only_beyond_20_points(after):
    goal = make_card("goal", facts={"team_wp_before_pct": 30, "team_wp_after_pct": after})
    tilt = {"en": "The whole match just tilted", "fr": "Tout le match vient de pencher", "es": "Todo el partido acaba de inclinarse"}
    for lang in LANGS:
        assert any(tilt[lang] in l for l in filled(goal, lang)) == (after - 30 > TILT_PTS), (lang, after)


def test_quietly_huge_is_for_saves_and_chances_and_no_line_takes_points():
    quiet = {"en": "Quietly huge", "fr": "L'air de rien", "es": "Sin hacer ruido"}
    verb = re.compile(r"(?i)\b(?:take|takes|gain|gains|prendre|prend|gagne\w*|ganar|gana|ganan|suma\w*)\s+(?:\w+\s+)?\{\w+\}\s+(?:points?|puntos?)\b")
    for kind, by_lang in LINES.items():
        for lang, slots in by_lang.items():
            for line in all_strings(slots):
                assert not verb.search(line), (kind, lang, line)
                assert quiet[lang] not in line or kind in ("save", "chance"), (kind, lang, line)


def test_the_fourth_goal_is_his_fourth_goal_of_the_match():
    words = {"en": "his fourth goal of the match", "fr": "son quatrième but du match", "es": "su cuarto gol del partido"}
    for lang in LANGS:
        lines = filled(make_card("goal", scorer_match_goals=4, minute="83'"), lang)
        assert any(words[lang] in l for l in lines) and not any("goal number" in l for l in lines), lang


# --- nothing said twice in a match ---------------------------------------------------------------------------

def test_no_duo_line_is_said_twice_in_a_match():
    memory = PhraseMemory()
    said = {lang: [] for lang in LANGS}
    for i in range(14):
        card = dict(make_card(score={"home": i, "away": 20}), id=f"g{i}")
        d = asyncio.run(draft(card, TEAMS, TemplateWriter(), LANGS, memory=memory, duo=True))
        assert d.report["verified"], d.report
        for l in d.duo:
            said[l["lang"]].append(PhraseMemory.key(l["line"]))
    for lang, lines in said.items():
        assert len(lines) == len(set(lines)), (lang, [l for l in lines if lines.count(l) > 1])


def test_no_duo_line_repeats_across_the_demo(demo):
    sink, _ = demo
    for lang in LANGS:
        lines = [PhraseMemory.key(l["line"]) for c in sink.cards.values() for l in c["duo"] + c.get("duo_ruling", []) if l["lang"] == lang]
        assert len(lines) == len(set(lines)), (lang, [l for l in lines if lines.count(l) > 1])


@pytest.fixture(scope="module")
def story_demo():
    """The demo with its storyline: the duo opens on the table, and the goals that change it tell it again."""
    from worker import storyline as sl

    stream, _ = generate_match(7, "demo")
    sink = MemorySink()
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True),
                        storyline_for=lambda match_id, teams: sl.build_storyline(9, 30, None, match_id, home=teams["home"], away=teams["away"], n_sims=1000))
    for m in stream:
        pipeline.handle(m)
    pipeline.flush()
    return sink


def test_no_duo_line_repeats_when_the_goals_tell_the_table(story_demo):
    docs = list(story_demo.cards.values()) + list(story_demo.insights.values())
    for lang in LANGS:
        lines = [PhraseMemory.key(l["line"]) for d in docs for l in d.get("duo", []) + d.get("duo_ruling", []) if l["lang"] == lang]
        assert len(lines) == len(set(lines)), (lang, [l for l in lines if lines.count(l) > 1])
        table = [l["line"] for c in story_demo.cards.values() for l in c["duo"] if l["lang"] == lang and l["line"].startswith(("If it stays", "Si ça reste", "Si se mantiene"))]
        assert len(table) >= 3, (lang, table)  # 14', 57', 63' (then ruled out) and 66' tell the table, each in its own words


def test_an_identical_duo_line_fails_and_a_new_version_keeps_what_the_old_one_said():
    memory = PhraseMemory()
    line = {"speaker": "wren", "lang": "en", "line": "And the table, Joss?"}
    memory.record("a", [], [line], version=1)
    assert [e["rule"] for e in memory.issues("b", [], [line])[0]] == ["repeated_line"]
    other = dict(line, line="Joss, what about the table?")
    memory.record("a", [], [other], version=2)  # the goal went to another player
    assert [e["rule"] for e in memory.issues("a", [], [line])[0]] == ["repeated_line"]  # version 1 may have been said
    assert memory.issues("a", [], [other])[0] == []  # a retry of version 2 is not a repeat


def test_a_model_duo_line_already_said_is_refused():
    memory = PhraseMemory()
    scorer = make_card()["player"]["name"]
    asyncio.run(draft(make_card(), TEAMS, FakeWriter([reply()]), ["en"], memory=memory, duo=True))
    writer = FakeWriter([reply(f"{scorer} again for Rovers"), reply(f"{scorer} once more for Rovers")])
    d = asyncio.run(draft(dict(make_card(), id="c-2"), TEAMS, writer, ["en"], memory=memory, duo=True))
    assert d.writer == "fake" and d.duo_writer == "template" and d.report["verified"] and "duo line already said" in writer.calls[1]["note"]


def test_after_a_goal_ruled_out_the_next_one_at_that_score_counts(demo):
    sink, _ = demo
    ruled, again = at(sink, "63'"), at(sink, "66'")
    assert ruled["status"] == "retracted" and ruled["score_before"] == again["score_before"]
    words = {"en": "this time it counts", "fr": "cette fois, ça compte", "es": "esta vez sí"}
    for lang in LANGS:
        first = next(l["line"] for l in again["duo"] if l["lang"] == lang)
        assert words[lang] in first.lower().replace("\xa0", " "), (lang, first)


# --- a goal credited to another player, a red card --------------------------------------------------------

def test_a_goal_credited_to_another_player_is_told_as_a_correction(demo):
    sink, _ = demo
    card = json.loads(json.dumps(at(sink, "31'")))  # the feed credits it to a team-mate after a deflection: version 2
    scorer = card["player"]["name"]
    previous = next(n for n in replay_names()[card["team"]] if n != scorer)
    assert card["version"] == 2 and "previous_scorer" not in card_facts(dict(card, previous_player=scorer), TEAMS)[0]
    card["previous_player"] = previous
    facts, _ = card_facts(card, TEAMS)
    assert facts["previous_scorer"] == previous and facts["scorer"] == scorer
    out = json.loads(TemplateWriter().write(card, facts, LANGS, memory=PhraseMemory(), duo=True))
    assert verify_duo(out["duo"], LANGS, facts)["verified"]
    for lang in LANGS:
        values, fields = template_values(card, facts, lang)
        corrections = {f[0] for t in LINES["reassigned"][lang]["heart"] if (f := fill(t, values, fields))}
        lines = [l["line"] for l in out["duo"] if l["lang"] == lang]
        assert lines[0] in corrections and scorer in lines[0], (lang, lines)  # the change of scorer, not the goal again
        for template in all_strings(LINES["reassigned"][lang]):  # every variant, whatever its condition
            line = fill(template, values | {c: "" for c in CONDITIONS}, fields)
            assert verify_duo([{"speaker": HEART, "lang": lang, "line": line[0], "claims": line[1]}, {"speaker": EYE, "lang": lang, "line": "Yes.", "claims": []}],
                              [lang], facts)["verified"], (lang, template)


@pytest.mark.parametrize("kind", ["second_yellow", "red", None])
def test_the_duo_says_how_the_player_was_sent_off(demo, kind):
    sink, _ = demo
    card = json.loads(json.dumps(next(c for c in sink.cards.values() if c["moment"] == "red_card")))  # a second yellow in the demo
    card.pop("card_kind", None)
    if kind:
        card["card_kind"] = kind
    facts, _ = card_facts(card, TEAMS)
    out = json.loads(TemplateWriter().write(card, facts, LANGS, memory=PhraseMemory(), duo=True))
    assert verify(json.dumps(out["texts"], ensure_ascii=False), LANGS, facts)[1]["verified"] and verify_duo(out["duo"], LANGS, facts)["verified"]
    for lang in LANGS:
        text = next(t for t in out["texts"] if t["lang"] == lang)
        assert card["player"]["name"] in text["headline"] + " " + text["why"], text
        heart = next(l["line"] for l in out["duo"] if l["lang"] == lang and l["speaker"] == HEART)
        for k, words in CARD_KIND_WORDS.items():
            assert bool(re.search(words[lang], heart)) == (k == facts.get("card_kind")), (lang, k, heart)
