"""The commentary duo, anti-repetition across a match, the product decisions on points and goals with no stake, and the insight path. No model call: crafted replies and templates only."""

import asyncio
import json
import re

import pytest

from generator import generate_match
from worker.desk import Desk
from worker.duo import EYE, HEART, LINES, PERSONAS, PhraseMemory, fill, persona_brief
from worker.pipeline import MemorySink, Pipeline
from worker.writer import (EXAMPLE_DUO, EXAMPLE_FACTS, INSIGHT_TEMPLATES, LOW_STAKE_TEMPLATES, TEMPLATES, WRITER_INSTRUCTIONS, TemplateWriter, card_facts, draft,
                           insight_facts, low_stake, ruling_facts, style_issues, template_values, verify, verify_duo, verify_reply, writer_request)

LANGS = ["en", "fr", "es"]
TEAMS = {"home": "Rovers", "away": "United"}


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
    ("red_card", make_card("red_card", "away"), {}),
    ("chance", make_card("chance", "away"), {}),
    ("ruling", make_card("goal", ruled_out={"reason": "offside", "margin_cm": 25}), {}),
]


@pytest.mark.parametrize("kind,card,extra", CASES, ids=[c[0] for c in CASES])
def test_every_duo_line_passes_the_verifier_with_its_numbers_claimed(kind, card, extra):
    facts, _ = ruling_facts(card, TEAMS) if kind == "ruling" else card_facts(card, TEAMS)
    facts |= extra
    for lang in LANGS:
        values, fields = template_values(card, facts, lang)
        values["subject"] = "Rovers"
        for template in all_strings(LINES[kind][lang]):
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
    assert any("fourth" in l or "goal number four" in l for l in en), en


def test_var_ruling_is_recorded_with_its_reason_and_margin(demo):
    sink, _ = demo
    offside, handball = at(sink, "63'"), at(sink, "69'")
    assert offside["ruled_out"] == {"reason": "offside", "cause": "var_offside", "minute_label": "64'", "margin_cm": 25}
    assert handball["ruled_out"]["reason"] == "handball" and handball["ruled_out"]["margin_cm"] is None
    for card in (offside, handball):
        facts, _ = ruling_facts(card, TEAMS)
        assert verify_duo(card["duo_ruling"], LANGS, facts)["verified"]
    assert any("25 cm" in l["line"].replace("centimetres", "cm").replace("centimètres", "cm").replace("centímetros", "cm") for l in offside["duo_ruling"] if l["lang"] == "en")
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


def test_write_insight_with_a_model_reply_and_thousands():
    texts = [{"headline": "Lightfoot, a first in the league", "why": "Dorian Lightfoot scores 4 as a substitute: 0 times in 18,620 matches before.", "lang": "en",
              "claims": [{"value": 4, "source_field": "goals_by_substitute"}, {"value": 0, "source_field": "times_before"}, {"value": 18620, "source_field": "past_matches"}]}]
    lines = [{"speaker": "wren", "lang": "en", "line": "Four from the bench! Is that a first, Joss?", "claims": []},
             {"speaker": "joss", "lang": "en", "line": "It is: 0 in 18,620 matches.", "claims": [{"value": 0, "source_field": "times_before"}, {"value": 18620, "source_field": "past_matches"}]}]
    writer = FakeWriter([json.dumps({"texts": texts, "duo": lines})])
    sink = MemorySink()
    doc = Desk(sink, writer, auto_approve=True, languages=("en",)).write_insight(json.loads(json.dumps(INSIGHT)), TEAMS)
    assert doc["writer"] == "fake" and doc["duo_writer"] == "fake" and doc["status"] == "approved" and doc["verification"]["verified"]
    assert writer.calls[0]["request"]["document"] == "insight"


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


def test_storyline_goes_through_the_same_path():
    story = {"id": "storyline-m", "type": "storyline", "match_id": "m", "facts": {"rovers_title_chance_pct": 12}, "field_meanings": {"rovers_title_chance_pct": "Rovers' chance of the title, in %"}}
    doc = Desk(MemorySink(), TemplateWriter(), auto_approve=True).write_insight(story, TEAMS)
    assert doc["status"] == "approved" and doc["verification"]["verified"] and len(doc["texts"]) == 3 and doc["duo"]


ENGINE_FACTS = {  # the headline facts the insight engine gives a substitute's four goals and a comeback
    "scorer-home-20-4": {"player": "Dorian Lightfoot", "team": "Rovers", "goals": 4, "goal_minutes": "52', 57', 66', 83'", "substitute": True, "came_on": "46'",
                         "history_count": 0, "history_opportunities": 19000, "history_since": "1976/77"},
    "comeback-home-2-3": {"team": "Rovers", "opponent": "United", "deficit_goals": 2, "trailing_score": "0-2", "stage": "won", "score": "5-2", "club_last_season": "2019/20",
                          "history_count": 114, "history_opportunities": 9579, "history_since": "1976/77", "last_season": "2025/26", "last_match": "Stellbury 4-3 Netherwick"},
}


def test_engine_insights_get_texts_with_their_numbers():
    for key, facts in ENGINE_FACTS.items():
        doc = {"id": f"insight-m-{key}", "type": "insight", "match_id": "m", "key": key, "kind": "rarity", "headline_facts": facts,
               "field_meanings": {k: k for k in facts}, "value": 80, "status": "proposed"}
        out = Desk(MemorySink(), TemplateWriter(), languages=LANGS).write_insight(doc, TEAMS)
        assert out["verification"]["verified"] and out["duo"]
        en = next(t for t in out["texts"] if t["lang"] == "en")
        assert {c["source_field"] for c in en["claims"]} >= {"history_opportunities"}  # the count behind the rarity is on screen
        fr = next(t for t in out["texts"] if t["lang"] == "fr")
        assert "9\u202f579" in fr["why"] or "19\u202f000" in fr["why"]


def test_a_starter_never_gets_the_bench_lines():
    facts = dict(ENGINE_FACTS["scorer-home-20-4"], substitute=False)
    facts.pop("came_on")
    doc = {"id": "insight-m-scorer-home-9-3", "type": "insight", "match_id": "m", "key": "scorer-home-9-3", "kind": "milestone", "headline_facts": facts,
           "field_meanings": {k: k for k in facts}, "value": 60, "status": "proposed"}
    out = Desk(MemorySink(), TemplateWriter(), languages=LANGS).write_insight(doc, TEAMS)
    text = json.dumps(out["texts"] + out["duo"], ensure_ascii=False).lower()
    assert not any(w in text for w in ("bench", "banc", "banquillo"))
