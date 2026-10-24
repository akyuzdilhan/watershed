import asyncio
import json

import pytest

from worker.writer import (EXAMPLE_FACTS, EXAMPLE_REPLY, LANGUAGE_RULES, STRUCTURAL_RULES, TEMPLATES, WRITER_INSTRUCTIONS, TemplateWriter, card_facts, draft,
                           style_issues, verify)

TEAMS = {"home": "Rovers", "away": "United"}
MOMENT_FACTS = {
    "goal": {"team_wp_before_pct": 30, "team_wp_after_pct": 54},
    "save": {"xg_pct": 20, "team_wp_before_pct": 81, "team_wp_if_goal_pct": 24},
    "red_card": {"team_wp_before_pct": 55, "team_wp_after_pct": 40},
    "chance": {"xg_pct": 40, "team_wp_before_pct": 40, "team_wp_if_goal_pct": 70},
}


def make_card(moment="goal", side="home", facts=None):
    return {"id": "c1", "version": 1, "match_id": "m", "team": side, "team_name": TEAMS[side], "moment": moment, "minute_label": "52'",
            "player": {"name": "Elio Ingleby"}, "score_before": {"home": 0, "away": 2}, "facts": facts or MOMENT_FACTS[moment]}


GOAL_FACTS, _ = card_facts(make_card(), TEAMS)  # Rovers goal: rovers_wp_before_pct 30, rovers_wp_after_pct 54, swing_pts 24
BEFORE, AFTER, SWING = ({"value": 30, "source_field": "rovers_wp_before_pct"}, {"value": 54, "source_field": "rovers_wp_after_pct"},
                        {"value": 24, "source_field": "swing_pts"})
GOAL_CLAIMS = [BEFORE, AFTER, SWING]


def rules(headline, why, lang, claims=GOAL_CLAIMS, facts=GOAL_FACTS):
    errors, warnings = style_issues(headline, why, lang, claims, facts)
    return {e["rule"] for e in errors}, {w["rule"] for w in warnings}


# --- each rule, good and bad, in each language ------------------------------------------------------

GOOD = {
    "en": ("Ingleby scores for Rovers", "Rovers go from 30% to 54% to win, up 24 points."),
    "fr": ("Ingleby marque pour Rovers", "Rovers passe de 30 % à 54 % de chances de gagner, soit 24 points de mieux."),
    "es": ("Gol de Ingleby para el Rovers", "El Rovers pasa del 30% al 54% de opciones de ganar: 24 puntos más."),
}

BAD = [  # (lang, headline, why, rule that must fail)
    ("fr", "Ingleby marque pour Rovers", "Rovers passe de 30 % à 54 % : swing de 24 points.", "forbidden_word"),
    ("fr", "Ingleby marque pour Rovers", "Rovers prend le momentum, de 30 % à 54 %, soit 24 points.", "forbidden_word"),
    ("es", "Gol de Ingleby para el Rovers", "El Rovers pasa del 30% al 54%: un swing de 24 puntos.", "forbidden_word"),
    ("es", "Gol de Ingleby para el Rovers", "Momentum del Rovers, del 30% al 54%: 24 puntos más.", "forbidden_word"),
    ("fr", "Ingleby relance l'United", "Rovers passe de 30 % à 54 %, soit 24 points de mieux.", "article_before_team"),
    ("fr", "Ingleby marque pour le Rovers", "Rovers passe de 30 % à 54 %, soit 24 points de mieux.", "article_before_team"),
    ("fr", "Ingleby punit la défense d'United", "Rovers passe de 30 % à 54 %, soit 24 points de mieux.", "article_before_team"),
    ("fr", "Ingleby, l’homme du Rovers", "Rovers passe de 30 % à 54 %, soit 24 points de mieux.", "article_before_team"),
    ("fr", "Ingleby marque", "Les chances des Rovers passent de 30 % à 54 %, soit 24 points de mieux.", "article_before_team"),
    ("en", "Ingleby scores", "Win chance jumped after the goal.", "probability_without_team"),
    ("en", "Ingleby scores", "The goal lifts their chances of winning.", "probability_without_team"),
    ("fr", "Ingleby marque", "Le but fait bondir les chances.", "probability_without_team"),
    ("es", "Gol de Ingleby", "El gol dispara las opciones de ganar.", "probability_without_team"),
    ("en", "Ingleby scores", "The goal takes them from 30% to 54%, up 24 points.", "probability_number_without_team"),
    ("fr", "Ingleby marque", "Le but les fait passer de 30 % à 54 %, soit 24 points.", "probability_number_without_team"),
    ("es", "Gol de Ingleby", "El gol los lleva del 30% al 54%: 24 puntos más.", "probability_number_without_team"),
    ("en", "Ingleby scores for Rovers", "Rovers go from 30% to 54% to win: a +24-point swing.", "signed_number"),
    ("fr", "Ingleby marque pour Rovers", "Rovers passe de 30 % à 54 % de chances de gagner : +24 points.", "signed_number"),
    ("es", "Gol de Ingleby para el Rovers", "El Rovers, con un +24 en puntos de diferencia, llega al 54%.", "signed_number"),
    ("en", "Ingleby scores for Rovers", "Rovers go from 30% to 54% to win, a jump of 24.", "points_unit_missing"),
    ("fr", "Ingleby marque pour Rovers", "Rovers passe de 30 % à 54 % de chances de gagner, un bond de 24.", "points_unit_missing"),
    ("es", "Gol de Ingleby para el Rovers", "El Rovers pasa del 30% al 54% de opciones de ganar: sube 24.", "points_unit_missing"),
    ("en", "Ingleby scores for Rovers", "Rovers now have 54 points of win probability, up 24 points.", "points_not_from_pts_field"),
    ("fr", "Ingleby marque pour Rovers", "Rovers atteint 54 points de probabilité, soit 24 points de mieux.", "points_not_from_pts_field"),
    ("es", "Gol de Ingleby para el Rovers", "El Rovers llega a 54 puntos de probabilidad: 24 puntos más.", "points_not_from_pts_field"),
    ("en", "52' Ingleby scores for Rovers", GOOD["en"][1], "headline_starts_with_number"),
    ("fr", "52' : Ingleby marque pour Rovers", GOOD["fr"][1], "headline_starts_with_number"),
    ("es", "52' Gol de Ingleby para el Rovers", GOOD["es"][1], "headline_starts_with_number"),
]


@pytest.mark.parametrize("lang", ["en", "fr", "es"])
def test_good_examples_have_no_style_issue(lang):
    assert rules(*GOOD[lang], lang) == (set(), set())


@pytest.mark.parametrize("lang,headline,why,rule", BAD)
def test_bad_examples_fail_their_rule(lang, headline, why, rule):
    errors, _ = rules(headline, why, lang)
    assert rule in errors


def test_swing_is_plain_english_but_forbidden_in_french_and_spanish():
    assert "forbidden_word" not in rules("Ingleby scores for Rovers", "Rovers go from 30% to 54% to win: a swing of 24 points.", "en")[0]


def test_club_names_without_article_pass_in_french():
    for headline in ("Ingleby marque pour United", "Ingleby punit United", "Le gardien de Rovers s'incline", "Ingleby face à United"):
        assert "article_before_team" not in rules(headline, GOOD["fr"][1], "fr")[0]


def test_spanish_keeps_its_article_before_a_club():
    assert rules("Gol de Ingleby para el Rovers", "El Rovers pasa del 30% al 54% de opciones de ganar: 24 puntos más.", "es") == (set(), set())


def test_decimal_percentage_fails_in_every_language():
    facts = dict(GOAL_FACTS, rovers_wp_after_pct=54.5)
    for lang, why in (("en", "Rovers now have 54.5% to win."), ("fr", "Rovers passe à 54,5 % de chances de gagner."), ("es", "El Rovers sube al 54,5% de opciones.")):
        assert "decimal_percent" in rules("Ingleby scores", why, lang, [{"value": 54.5, "source_field": "rovers_wp_after_pct"}], facts)[0]


def test_warnings_do_not_fail():
    cases = [
        ("en", "Rovers level at 52'", GOOD["en"][1], "minute_in_headline"),
        ("fr", "Ingleby marque à la 52e minute", GOOD["fr"][1], "minute_in_headline"),
        ("es", "Gol de Ingleby en el minuto 52", GOOD["es"][1], "minute_in_headline"),
        ("en", "Ingleby scores", "Rovers' win chance goes from 30% to 54%, up 24 points.", "jargon"),
        ("fr", "Ingleby marque pour Rovers", "Rovers passe de 30% à 54% de chances de gagner, soit 24 points de mieux.", "percent_without_space"),
        ("fr", "Quel goal de Ingleby pour Rovers", GOOD["fr"][1], "anglicism"),
        ("es", "Gol de Ingleby para el Rovers", "El Rovers pasa del 30% al 54% de opciones de ganar, 24 en puntos de diferencia.", "clumsy_phrase"),
        ("en", "Elio Ingleby scores a wonderful goal for Rovers today", GOOD["en"][1], "too_long"),
    ]
    for lang, headline, why, rule in cases:
        errors, warnings = rules(headline, why, lang)
        assert rule in warnings and rule not in errors, (lang, rule, errors, warnings)


def test_rules_are_data_with_a_known_severity():
    assert set(LANGUAGE_RULES) == {"en", "fr", "es"}
    for entries in LANGUAGE_RULES.values():
        assert all(severity in ("error", "warning") for _, severity, _ in entries)
    assert all(severity in ("error", "warning") for severity, _ in STRUCTURAL_RULES.values())


# --- the Verifier uses the style rules --------------------------------------------------------------

def reply(lang, headline, why, claims=GOAL_CLAIMS):
    return json.dumps([{"headline": headline, "why": why, "lang": lang, "claims": claims}], ensure_ascii=False)


@pytest.mark.parametrize("lang", ["en", "fr", "es"])
def test_style_error_fails_verification_and_warning_does_not(lang):
    assert verify(reply(lang, *GOOD[lang]), [lang], GOAL_FACTS)[1]["verified"]
    _, headline, why, _ = next(b for b in BAD if b[0] == lang and b[3] == "headline_starts_with_number")
    _, report = verify(reply(lang, headline, why), [lang], GOAL_FACTS)
    assert not report["verified"] and report["cards"][0]["style_errors"]
    long_headline = GOOD[lang][0] + " encore une fois ce soir devant tous"
    _, report = verify(reply(lang, long_headline, GOOD[lang][1]), [lang], GOAL_FACTS)
    assert report["verified"] and report["cards"][0]["style_warnings"]


def test_the_live_run_examples_now_fail():
    cases = [
        ("fr", "Ingleby relance Rovers", "Le but fait bondir les chances : swing de 24 points.", [SWING]),
        ("fr", "Ingleby punit l'United", "Rovers passe de 30 % à 54 %, soit 24 points de mieux.", GOAL_CLAIMS),
        ("en", "Ingleby scores", "Win chance jumped to 54%: a +24-point swing.", [AFTER, SWING]),
        ("es", "Gol de Ingleby", "El Rovers llega al 54%, con un +24 en puntos de diferencia.", [AFTER, SWING]),
        ("en", "52' Rovers pull one back", GOOD["en"][1], GOAL_CLAIMS + [{"value": 52, "source_field": "minute"}]),
    ]
    for lang, headline, why, claims in cases:
        assert not verify(reply(lang, headline, why, claims), [lang], GOAL_FACTS)[1]["verified"], (lang, headline, why)


def test_number_checks_are_unchanged():
    _, report = verify(reply("en", "Ingleby scores for Rovers", "Rovers go from 30% to 61% to win, up 24 points."), ["en"], GOAL_FACTS)
    assert report["cards"][0]["unclaimed_numbers"] == [61.0] and not report["verified"]
    _, report = verify(reply("en", "Ingleby scores for Rovers", "Rovers go from 30% to 54% to win, up 24%."), ["en"], GOAL_FACTS)
    assert report["cards"][0]["wrong_unit"] == [24.0] and not report["verified"]


# --- the instructions and the templates -------------------------------------------------------------

def test_instruction_example_passes_the_verifier():
    _, report = verify(json.dumps(EXAMPLE_REPLY, ensure_ascii=False), ["en", "fr", "es"], EXAMPLE_FACTS)
    assert report["verified"]
    assert all(not c["style_errors"] and not c["style_warnings"] for c in report["cards"])
    assert json.dumps(EXAMPLE_REPLY, ensure_ascii=False) in WRITER_INSTRUCTIONS


def test_instructions_keep_the_output_format():
    assert '{"headline": string (max 8 words), "why": string (max 25 words), "lang": string, "claims": [{"value": number, "source_field": string}]}' in WRITER_INSTRUCTIONS
    assert "get_moment" in WRITER_INSTRUCTIONS


@pytest.mark.parametrize("moment", sorted(TEMPLATES))
@pytest.mark.parametrize("side", ["home", "away"])
def test_templates_pass_every_check(moment, side):
    card = make_card(moment, side)
    facts, _ = card_facts(card, TEAMS)
    texts, report = verify(TemplateWriter().write(card, facts, ["en", "fr", "es"]), ["en", "fr", "es"], facts)
    assert report["verified"]
    for entry in report["cards"]:
        assert not entry["style_errors"] and not entry["style_warnings"] and not entry["unknown_names"], entry
    assert all(TEAMS[side] in t["why"] for t in texts)


@pytest.mark.parametrize("moment", sorted(TEMPLATES))
def test_templates_pass_at_the_probability_bounds(moment):
    extreme = {k: (99 if i % 2 else 1) if k != "xg_pct" else 5 for i, k in enumerate(MOMENT_FACTS[moment])}
    card = make_card(moment, "away", extreme)
    facts, _ = card_facts(card, TEAMS)
    assert verify(TemplateWriter().write(card, facts, ["en", "fr", "es"]), ["en", "fr", "es"], facts)[1]["verified"]


# --- the retry ----------------------------------------------------------------------------------------

class FakeWriter:
    """Replays crafted replies: no model call."""

    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.notes = []

    async def write_async(self, card, facts, languages, note=None):
        self.notes.append(note)
        return self.replies.pop(0)


def test_a_style_failure_is_retried_with_feedback():
    bad = reply("fr", "Ingleby punit l'United", GOOD["fr"][1])
    good = reply("fr", *GOOD["fr"])
    writer = FakeWriter([bad, good])
    d = asyncio.run(draft(make_card(), TEAMS, writer, ["fr"]))
    assert d.writer == "fake" and d.attempts == 2 and d.report["verified"]
    assert writer.notes[0] is None and "article_before_team" in writer.notes[1]


def test_two_style_failures_fall_back_to_the_template():
    bad = reply("en", "Ingleby scores", "Win chance jumped: a +24-point swing.", [SWING])
    d = asyncio.run(draft(make_card(), TEAMS, FakeWriter([bad, bad]), ["en"]))
    assert d.writer == "template" and d.attempts == 2 and d.report["verified"]
