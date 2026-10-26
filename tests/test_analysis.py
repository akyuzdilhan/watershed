import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from web.matchfeed import build, load
from worker.analysis import (ANALYST_EXAMPLE, ANALYST_INSTRUCTIONS, HOW, NOTES, AnalystDesk, AnalystWriter, analyse_async, analysis_facts, defenders_taken_out, dry_analyst, feed_actions, fill_note, note_scores,
                             parse_analysis, template_analysis, verify_analysis)
from worker.desk import Desk
from worker.pipeline import MemorySink, Pipeline
from worker.writer import EM_DASH, TemplateWriter

ROOT = Path(__file__).resolve().parents[1]
REPLAY = ROOT / "data" / "replay" / "demo-0007.jsonl.gz"
LANGS = ["en", "fr", "es"]
GOAL_MINUTES = ["14'", "31'", "52'", "57'", "63'", "66'", "69'", "83'", "88'"]


@pytest.fixture(scope="module")
def messages():
    return load(REPLAY)


@pytest.fixture(scope="module")
def feed(messages):
    return build(messages)["actions"]


def goal_cards(feed):
    return [{"id": f"card-{a['event_id']}", "type": "card", "match_id": a["match_id"], "event_id": a["event_id"], "shot_event_id": a["event_id"], "moment": "goal",
             "minute_label": a["minute_label"], "version": 1, "team": a["team"], "player": a["player"]} for a in feed if a.get("shot") and a["shot"]["outcome"] == "goal"]


def facts_at(feed, minute):
    return analysis_facts(feed, next(c for c in goal_cards(feed) if c["minute_label"] == minute))


# --- facts ----------------------------------------------------------------------------------------

def test_every_goal_of_the_demo_has_its_facts_each_with_a_meaning(feed):
    cards = goal_cards(feed)
    assert [c["minute_label"] for c in cards] == GOAL_MINUTES
    for card in cards:
        facts, meanings = analysis_facts(feed, card)
        assert set(facts) == set(meanings) and "minute" not in facts and facts["moment"] == "goal"
        assert not any(w in k or w in m for k, m in meanings.items() for w in ("median", "recent", "window"))  # no field invites a word the Verifier refuses
        assert facts["passes_in_move"] >= 2 and facts["move_duration_s"] > 0 and 0 < facts["xg_pct"] < 100
        assert all(isinstance(v, (str, int, float)) for v in facts.values())


def test_defenders_taken_out_stay_credible_on_the_demo_goals(feed):
    taken = {c["minute_label"]: analysis_facts(feed, c)[0].get("best_pass_defenders_taken_out") for c in goal_cards(feed)}
    assert taken == {"14'": 2, "31'": 2, "52'": 3, "57'": 3, "63'": 2, "66'": 3, "69'": 2, "83'": 4, "88'": 3}


def test_a_short_pass_takes_nobody_out():
    frame = [{"player_id": "d1", "teammate": False, "location": [40.0, 30.0]}, {"player_id": "me", "teammate": True, "actor": True, "location": [30.0, 30.0]}]
    after = [{"player_id": "d1", "teammate": False, "location": [36.0, 30.0]}, {"player_id": "you", "teammate": True, "actor": True, "location": [39.0, 30.0]}]
    short = {"start_x": 30.0, "end_x": 39.0, "freeze_frame": frame}
    longer = {"start_x": 30.0, "end_x": 41.0, "freeze_frame": frame}
    reception = {"start_x": 39.0, "freeze_frame": after}
    assert defenders_taken_out(short, reception) == [] and defenders_taken_out(longer, reception) == ["d1"]


def test_the_52nd_minute_goal_is_measured_on_the_move(feed):
    facts, _ = facts_at(feed, "52'")
    assert (facts["team"], facts["opponent"], facts["regain_how"], facts["regain_zone"], facts["high_regain"]) == ("Varleigh", "Yarnford", "tackle", "own_third", False)
    assert (facts["regain_from_own_goal_m"], facts["passes_in_move"], facts["move_duration_s"], facts["best_pass_defenders_taken_out"]) == (19, 5, 12, 3)
    assert facts["opponent_line_moved"] == "deeper" and facts["team_threat_trend"] == "up" and facts["team_threat_share_shift_pts"] >= 15


def test_only_the_actions_before_the_shot_count(feed):
    card = next(c for c in goal_cards(feed) if c["minute_label"] == "52'")
    shot = next(a for a in feed if a["event_id"] == card["event_id"])
    assert analysis_facts(feed, card) == analysis_facts([a for a in feed if a["seq"] <= shot["seq"]], card)


def test_a_card_that_is_not_a_shot_has_no_analysis(feed):
    card = goal_cards(feed)[0]
    assert analysis_facts(feed, card | {"moment": "red_card"}) is None and analysis_facts(feed, card | {"shot_event_id": "nope", "event_id": "nope"}) is None


def test_feed_actions_reads_the_match_as_the_feed_does(messages, feed):
    from worker.match import Match

    match = Match("demo-0007")
    for m in messages:
        match.receive(m)
    match.flush()
    mine = feed_actions(match)
    assert [a["event_id"] for a in mine] == [a["event_id"] for a in feed]
    assert {a["event_id"] for a in mine if a.get("voided")} == {a["event_id"] for a in feed if a.get("voided")} and len(match.voided) == 2


# --- template and Verifier ------------------------------------------------------------------------

def test_the_template_notes_verify_in_three_languages_for_every_goal(feed):
    for card in goal_cards(feed):
        facts, _ = analysis_facts(feed, card)
        texts = template_analysis(facts, LANGS)
        assert sorted(texts) == sorted(LANGS) and all(len(texts[lang]) == 2 for lang in LANGS)
        report = verify_analysis(texts, LANGS, facts)
        assert report["verified"], [e for e in report["lines"] if not e["verified"]]
        assert all(EM_DASH not in l["line"] for lines in texts.values() for l in lines)


def test_the_notes_are_the_strongest_facts_in_the_same_order_in_every_language(feed):
    facts, _ = facts_at(feed, "52'")
    keys = [k for _, k in note_scores(facts)][:2]
    texts = template_analysis(facts, LANGS)
    assert keys == ["threat_up", "counter"]
    for lang in LANGS:
        assert [sorted(c["source_field"] for c in l["claims"]) for l in texts[lang]] == [
            ["team_threat_share_before_shot_pct", "team_threat_share_earlier_pct"], ["move_duration_s", "passes_in_move", "regain_from_own_goal_m"]]
    assert "63%" in texts["en"][0]["line"] and "63 %" in texts["fr"][0]["line"] and "28%" in texts["es"][0]["line"]


def test_french_and_spanish_decimals_use_a_comma():
    facts = {"shooter": "Ari Holt", "shooter_space_m": 6.1, "typical_space_m": 2.3, "home_team": "Varleigh", "away_team": "Yarnford"}
    lines = {lang: template_analysis(facts, [lang], count=1)[lang][0]["line"] for lang in LANGS}
    assert "6.1 m" in lines["en"] and "6,1 m" in lines["fr"] and "6,1 m" in lines["es"]
    assert all(verify_analysis({lang: template_analysis(facts, [lang], count=2)[lang]}, [lang], facts)["languages"][lang]["count_ok"] is False for lang in LANGS)


def test_every_note_verifies_in_every_language_when_its_facts_are_there(feed):
    facts, _ = facts_at(feed, "52'")
    facts = facts | {"high_regain": True, "shooter_space_m": 6.1, "typical_space_m": 2.3, "opponent_line_moved": "higher", "opponent_line_gap_m": 4}
    cases = [(key, how) for key in NOTES for how in (HOW["en"] if key in ("high_regain", "counter", "build_up", "quick") else ["tackle"])]
    for key, how in cases:
        for lang in LANGS:
            note = fill_note(NOTES[key][lang], facts | {"regain_how": how, "regain_player": "Otto Meyer"}, lang)
            assert note is not None, (key, how, lang)
            report = verify_analysis({lang: [{"line": note[0], "claims": note[1]}] * 2}, [lang], facts | {"regain_player": "Otto Meyer"})
            assert report["verified"], (key, how, lang, report["lines"][0])


def good(feed):
    facts, _ = facts_at(feed, "52'")
    return facts, template_analysis(facts, LANGS)


@pytest.mark.parametrize("lang,line,claims,rule", [
    ("en", "Varleigh won it after 99 passes.", [], "unclaimed_numbers"),
    ("en", "Varleigh played 7 passes.", [{"value": 7, "source_field": "passes_in_move"}], "bad_claims"),
    ("en", "Varleigh were 12% faster.", [{"value": 12, "source_field": "move_duration_s"}], "wrong_unit"),
    ("en", f"Varleigh broke fast {EM_DASH} 12 s to the shot.", [{"value": 12, "source_field": "move_duration_s"}], "em_dash"),
    ("en", "Varleigh were onside, the runner offside by 12 cm before.", [{"value": 12, "source_field": "move_duration_s"}], "offside_margin"),
    ("fr", "Varleigh, hors-jeu de 12 m avant.", [{"value": 12, "source_field": "move_duration_s"}], "offside_margin"),
    ("en", "Varleigh take 35 points from this spell.", [{"value": 35, "source_field": "team_threat_share_shift_pts"}], "league_points_verb"),
    ("fr", "Varleigh va prendre 35 points sur cette période.", [{"value": 35, "source_field": "team_threat_share_shift_pts"}], "league_points_verb"),
    ("es", "El Varleigh puede ganar 35 puntos aquí.", [{"value": 35, "source_field": "team_threat_share_shift_pts"}], "league_points_verb"),
    ("en", "Varleigh were 63 points ahead.", [{"value": 63, "source_field": "team_threat_share_before_shot_pct"}], "points_not_from_pts_field"),
    ("en", "A move like that comes once in 12 matches for Varleigh.", [{"value": 12, "source_field": "move_duration_s"}], "league_rate"),
    ("en", "Varleigh were 5 times rarer than that.", [{"value": 5, "source_field": "passes_in_move"}], "league_rate"),
    ("en", "That move is worth 5 bits for Varleigh.", [{"value": 5, "source_field": "passes_in_move"}], "league_rate"),
    ("fr", "Varleigh a le momentum.", [], "forbidden_word"),
    ("es", "Swing total para el Varleigh.", [], "forbidden_word"),
    ("fr", "La pression de l'Yarnford a cédé.", [], "article_before_team"),
    ("en", "Varleigh played it like Erling Haaland.", [], "unknown_names"),
    ("en", "Varleigh's line played higher (34 m, 31 m).", [{"value": 34, "source_field": "regain_from_own_goal_m"}], "data_dump"),
    ("en", "Varleigh had space against a 2.8 m match median.", [{"value": 2.8, "source_field": "typical_space_m"}], "field_word"),
    ("en", "Varleigh's recent line was deep.", [], "field_word"),
    ("en", "Varleigh's line moved, change 3.", [{"value": 3, "source_field": "best_pass_defenders_taken_out"}], "field_word"),
    ("en", "Varleigh's opponent_line_before_shot_m was low.", [], "field_word"),
    ("fr", "Varleigh avait de l'espace, contre une médiane faible.", [], "field_word"),
    ("en", "Varleigh won it 19 m out, then 5 passes in 12 s.", [{"value": 19, "source_field": "regain_from_own_goal_m"}, {"value": 5, "source_field": "passes_in_move"},
                                                            {"value": 12, "source_field": "move_duration_s"}], "too_many_numbers"),
])
def test_the_verifier_refuses_a_bad_analyst_line(feed, lang, line, claims, rule):
    facts, texts = good(feed)
    texts = {**texts, lang: [texts[lang][0], {"line": line, "claims": claims}]}
    report = verify_analysis(texts, LANGS, facts)
    bad = [e for e in report["lines"] if not e["verified"]]
    assert not report["verified"] and len(bad) == 1 and bad[0]["lang"] == lang
    if rule in ("unclaimed_numbers", "bad_claims", "wrong_unit", "unknown_names"):
        assert bad[0][rule]
    else:
        assert rule in {i["rule"] for i in bad[0]["style_errors"]}


def test_two_or_three_notes_per_language(feed):
    facts, texts = good(feed)
    assert verify_analysis(texts, LANGS, facts)["verified"]
    assert not verify_analysis({**texts, "fr": texts["fr"][:1]}, LANGS, facts)["verified"]
    assert not verify_analysis({**texts, "es": texts["es"] * 2}, LANGS, facts)["verified"]
    assert not verify_analysis({k: v for k, v in texts.items() if k != "es"}, LANGS, facts)["verified"]


def test_a_reply_has_its_em_dashes_normalised_like_every_model_text():
    parsed, valid = parse_analysis('Here: {"en": [{"line": "Fast ' + EM_DASH + ' very fast.", "claims": []}]}')
    assert not valid and parsed["en"][0]["line"] == "Fast, very fast."
    assert parse_analysis("[]") == ({}, False)


def test_the_instructions_name_the_on_air_rules():
    from worker.writer import WRITER_INSTRUCTIONS

    for rule in ("At most 2 numbers in a sentence", "Never write the minute", "Never the words median, médiane, mediana, recent", "Never numbers in brackets",
                 "There is no minute field"):
        assert rule in ANALYST_INSTRUCTIONS, rule
    for lang in ("fr", "es"):  # the Writer's rule on clubs, word for word
        line = next(l for l in WRITER_INSTRUCTIONS.splitlines() if l.startswith(f"- {lang}:"))
        assert line in ANALYST_INSTRUCTIONS
    assert "<writer_rules>" not in ANALYST_INSTRUCTIONS and json.dumps(ANALYST_EXAMPLE, ensure_ascii=False) in ANALYST_INSTRUCTIONS
    assert EM_DASH not in ANALYST_INSTRUCTIONS and "by a whisker" in ANALYST_INSTRUCTIONS and "d'un rien" in ANALYST_INSTRUCTIONS and "get_analysis_facts" in ANALYST_INSTRUCTIONS
    assert all(EM_DASH not in t for by_lang in NOTES.values() for t in by_lang.values())


# --- the agent ------------------------------------------------------------------------------------

class Reply:
    def __init__(self, text, finish_reason="stop"):
        self.text, self.finish_reason, self.usage_details = text, finish_reason, {"input_token_count": 900, "output_token_count": 200}


class FakeAgent:
    """Replays replies or raises; records each prompt. No model call."""

    def __init__(self, *steps):
        self.steps, self.prompts = list(steps), []

    async def run(self, prompt, options=None, **_):
        self.prompts.append(prompt)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return Reply(step)


def run_analysis(feed, *steps, calls_left=2):
    facts, meanings = facts_at(feed, "52'")
    agent = FakeAgent(*steps)
    writer = AnalystWriter(model="analyst-test", agent=agent)
    doc = asyncio.run(analyse_async({"id": "card-x", "version": 1}, facts, meanings, writer, LANGS, calls_left=calls_left))
    return doc, agent, facts


def test_the_model_notes_are_kept_once_verified(feed):
    facts, texts = good(feed)
    mine = {"line": f"{facts['team']} won it {facts['regain_from_own_goal_m']} m from their own goal.", "claims": [{"value": 19, "source_field": "regain_from_own_goal_m"}]}
    model = {**texts, "en": [texts["en"][0], mine]}
    doc, agent, _ = run_analysis(feed, json.dumps(model, ensure_ascii=False))
    assert doc["writer"] == "analyst-test" and doc["verified"] and doc["texts"] == model and doc["verification"]["attempts"] == 1
    assert doc["verification"]["fallback_reason"] is None and doc["writer_config"]["deployment"] == "analyst-test"
    assert '"card_id": "card-x"' in agent.prompts[0] and agent.prompts[0].endswith("from get_analysis_facts only.")


def test_one_retry_told_what_was_rejected_then_the_template(feed):
    facts, texts = good(feed)
    bad = json.dumps({**texts, "en": [texts["en"][0], {"line": "Varleigh had 99 passes.", "claims": []}]}, ensure_ascii=False)
    doc, agent, _ = run_analysis(feed, bad, json.dumps(texts, ensure_ascii=False))
    assert doc["verified"] and doc["verification"]["attempts"] == 2 and "unclaimed_numbers: [99.0]" in agent.prompts[1]
    doc, _, _ = run_analysis(feed, bad, bad)
    assert doc["writer"] == "template" and doc["verified"] and doc["verification"]["fallback_reason"] == "verifier" and doc["texts"] == texts


def test_a_timeout_goes_to_the_template_at_once(feed):
    doc, agent, _ = run_analysis(feed, TimeoutError())
    assert doc["writer"] == "template" and doc["verification"]["attempts"] == 1 and doc["verification"]["fallback_reason"] == "timeout" and len(agent.prompts) == 1


def test_the_example_of_the_instructions_passes_the_verifier_on_the_real_facts(feed):
    facts, _ = facts_at(feed, "52'")
    report = verify_analysis(ANALYST_EXAMPLE, LANGS, facts)
    assert report["verified"], [e for e in report["lines"] if not e["verified"]]
    assert all(len(re.findall(r"[.!?](?:\s|$)", l["line"])) <= 2 for lines in ANALYST_EXAMPLE.values() for l in lines)
    assert all(sum(len(re.findall(r"[.!?](?:\s|$)", l["line"])) for l in lines) == 3 for lines in ANALYST_EXAMPLE.values())


def test_the_retry_carries_every_problem_and_what_the_rule_means(feed):
    facts, texts = good(feed)
    bad = {**texts, "en": [texts["en"][0], {"line": "At 52', Varleigh went 19 m in 12 s with 5 passes.", "claims": [{"value": "52'", "source_field": "minute"}]}],
           "fr": [texts["fr"][0], {"line": "Le Yarnford recule face à Varleigh.", "claims": []}]}
    doc, agent, _ = run_analysis(feed, json.dumps(bad, ensure_ascii=False), json.dumps(texts, ensure_ascii=False))
    note = agent.prompts[1]
    assert doc["verified"] and doc["verification"]["attempts"] == 2
    assert "en line 2" in note and "too_many_numbers" in note and "article_before_team: Le Yarnford" in note and "unclaimed_numbers" in note
    assert "Fix: " in note and "every digit group counted" in note and "never write the minute" in note and "make the club the subject" in note


def test_the_analyst_takes_its_own_model_effort_and_budget(monkeypatch):
    for key in ("FOUNDRY_MODEL", "FOUNDRY_REASONING_EFFORT", "FOUNDRY_MAX_TOKENS", "FOUNDRY_ANALYST_MODEL", "FOUNDRY_ANALYST_REASONING_EFFORT", "FOUNDRY_ANALYST_MAX_TOKENS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FOUNDRY_MODEL", "writer-model")
    monkeypatch.setenv("FOUNDRY_REASONING_EFFORT", "medium")
    monkeypatch.setenv("FOUNDRY_MAX_TOKENS", "5000")
    a = AnalystWriter(agent=object())
    assert (a.model, a.effort, a.max_tokens) == ("writer-model", "medium", 5000)
    monkeypatch.setenv("FOUNDRY_ANALYST_MODEL", "analyst-model")
    monkeypatch.setenv("FOUNDRY_ANALYST_REASONING_EFFORT", "low")
    monkeypatch.setenv("FOUNDRY_ANALYST_MAX_TOKENS", "2000")
    a = AnalystWriter(agent=object())
    assert (a.model, a.effort, a.max_tokens, a.agent_name) == ("analyst-model", "low", 2000, "analyst") and a.config["max_tokens"] == 2000


def test_the_tool_serves_the_facts_and_their_meanings(feed):
    facts, meanings = facts_at(feed, "52'")
    writer = AnalystWriter(model="m", agent=object())
    writer.facts["card-x"] = (facts, meanings)
    tool = writer.tool()
    fn = getattr(tool, "func", None) or getattr(tool, "_func", None) or tool
    assert getattr(tool, "name", "get_analysis_facts") == "get_analysis_facts"
    assert json.loads(fn(card_id="card-x")) == {"facts": facts, "field_meanings": meanings} and "error" in json.loads(fn(card_id="nope"))


# --- on the desk ----------------------------------------------------------------------------------

def run_demo(messages, analyst=None, calls=12):
    sink = MemorySink()
    holder = {}
    desk = AnalystDesk(Desk(sink, TemplateWriter(), auto_approve=True), lambda match_id: feed_actions(holder["p"].matches[match_id]), analyst, max_calls_per_match=calls)
    pipeline = holder["p"] = Pipeline(sink=sink, desk=desk)
    for m in messages:
        pipeline.handle(m)
    pipeline.flush()
    desk.close(None)
    return sink, desk


@pytest.fixture(scope="module")
def demo(messages):
    return run_demo(messages)


def test_every_goal_and_the_save_get_verified_notes_and_the_red_card_none(demo):
    sink, _ = demo
    cards = sorted(sink.cards.values(), key=lambda c: c["clock_s"])
    with_notes = [c["minute_label"] for c in cards if "analysis" in c]
    assert with_notes == GOAL_MINUTES[:7] + ["72'"] + GOAL_MINUTES[7:]
    for c in cards:
        if "analysis" in c:
            a = c["analysis"]
            assert a["verified"] and a["writer"] == "template" and a["card_version"] == c["version"] and a["facts_version"] == 3
            assert sorted(a["texts"]) == sorted(LANGS) and a["machine_translated"] == ["es"] and set(a["facts"]) == set(a["field_meanings"])


def test_a_goal_ruled_out_keeps_its_notes_marked_retracted(demo):
    sink, _ = demo
    by_minute = {c["minute_label"]: c for c in sink.cards.values()}
    for minute in ("63'", "69'"):
        assert by_minute[minute]["status"] == "retracted" and by_minute[minute]["analysis"]["status"] == "retracted"
        assert by_minute[minute]["analysis"]["retracted_cause"].startswith("var_")
    assert all(c["analysis"]["status"] == "proposed" for m, c in by_minute.items() if m not in ("63'", "69'") and "analysis" in c)
    actions = [(e["card_id"], e["action"]) for e in sink.audit]
    for minute in ("63'", "69'"):
        mine = [a for cid, a in actions if cid == by_minute[minute]["id"]]
        assert mine.index("analysis_written") < mine.index("retracted") < mine.index("analysis_retracted")


def test_a_rewrite_gets_notes_for_its_new_version(demo):
    sink, _ = demo
    rewritten = [c for c in sink.cards.values() if c["version"] > 1]
    assert rewritten and all(c["analysis"]["card_version"] == c["version"] for c in rewritten)


def test_the_desk_stays_reachable_through_the_wrapper(demo):
    sink, desk = demo
    card = next(c for c in sink.cards.values() if c["status"] == "approved")
    assert desk.max_calls_per_match == 40 and desk.calls.get("demo-0007", 0) == 0 and desk.languages == LANGS
    desk.reject(card, "producer")
    assert sink.cards[card["id"]]["status"] == "rejected"


def test_the_dry_analyst_runs_the_model_path_off_the_critical_path(messages):
    sink, desk = run_demo(messages, dry_analyst())
    notes = [c["analysis"] for c in sink.cards.values() if "analysis" in c]
    assert len(notes) == 10 and all(a["verified"] for a in notes)
    assert all(a["writer"] == "dry" for a in notes) and desk.analyst_calls["demo-0007"] <= 12
    assert {c["minute_label"] for c in sink.cards.values() if c.get("analysis", {}).get("status") == "retracted"} == {"63'", "69'"}


def test_the_analyst_has_a_budget_of_its_own(messages):
    sink, desk = run_demo(messages, dry_analyst(), calls=3)
    notes = [c["analysis"] for c in sink.cards.values() if "analysis" in c]
    by_model = [a for a in notes if a["writer"] == "dry"]
    assert desk.analyst_calls["demo-0007"] == 3 and desk.calls.get("demo-0007", 0) == 0  # one call each: nothing held for a retry
    assert 2 <= len(by_model) <= 3  # a call made for the 31' card before the feed rewrote it is spent on a version that is dropped
    assert all(a["verification"]["fallback_reason"] == "budget" for a in notes if a["writer"] == "template")


class Refused:
    """A model whose replies the Verifier always refuses: every job spends both its calls, when it has two."""

    name = "refused"

    def __init__(self) -> None:
        self.calls = 0

    async def write_async(self, card_id, facts, languages, note=None, max_tokens=None):
        self.calls += 1
        return "not json"


def test_a_call_counts_when_it_is_made_and_the_cap_holds(messages):
    writer = Refused()
    sink, desk = run_demo(messages, writer, calls=3)
    notes = sorted((c["analysis"] for c in sink.cards.values() if "analysis" in c), key=lambda a: a["verification"]["attempts"], reverse=True)
    assert writer.calls == 3 and desk.analyst_calls["demo-0007"] == 3
    assert notes[0]["verification"]["attempts"] == 2 and sum(a["verification"]["attempts"] for a in notes) <= 3 and all(a["writer"] == "template" for a in notes)
    assert sum(1 for a in notes if a["verification"]["fallback_reason"] == "budget") >= len(notes) - 2


def test_notes_finished_after_a_retraction_are_marked_retracted(feed):
    sink = MemorySink()
    desk = AnalystDesk(Desk(sink, TemplateWriter()), lambda _: feed, dry_analyst())
    card = next(c for c in goal_cards(feed) if c["minute_label"] == "63'") | {"status": "proposed", "clock_s": 0, "team_name": "Varleigh",
                                                                               "score_before": {"home": 0, "away": 2}, "value_pts": 40,
                                                                               "facts": {"xg_pct": 26, "team_wp_before_pct": 20, "team_wp_after_pct": 40, "draw_before_pct": 20, "draw_after_pct": 30}}
    teams = {"home": "Varleigh", "away": "Yarnford"}
    desk.write(card, teams)
    desk.on_action(card, {"action": "retract", "cause": "var_offside", "previous_status": "pending", "minute_label": "64'"}, teams)
    assert "analysis" not in card  # still running: marked when applied
    desk.close(None)
    assert card["analysis"]["status"] == "retracted" and card["analysis"]["writer"] == "dry" and sink.cards[card["id"]]["analysis"]["status"] == "retracted"


# --- the command ----------------------------------------------------------------------------------

def worker(tmp_path, name, *extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FOUNDRY_", "AZURE_"))} | {"PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    run = subprocess.run([sys.executable, "-m", "worker", str(REPLAY), "--writer", "template", "--desk-dir", str(tmp_path / name), *extra],
                         capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
    assert run.returncode == 0, run.stderr[-2000:]
    return run.stdout, json.loads((tmp_path / name / "demo-0007" / "cards.json").read_text(encoding="utf-8"))


def test_analyst_off_changes_nothing_and_imports_nothing(tmp_path):
    out_default, cards_default = worker(tmp_path, "default")
    out_off, cards_off = worker(tmp_path, "off", "--analyst", "off")
    assert out_default == out_off and cards_default == cards_off and not any("analysis" in c for c in cards_off)
    code = "import sys, worker.__main__, worker.desk, worker.pipeline; print('worker.analysis' in sys.modules)"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env).stdout.strip() == "False"


def test_the_worker_runs_the_dry_analyst_end_to_end(tmp_path):
    out, cards = worker(tmp_path, "dry", "--analyst", "foundry", "--analyst-dry")
    final = out.splitlines()[-1]
    assert final.startswith("analyst (dry): 10 by the model, 0 template, ") and final.endswith(" of 12 calls"), final
    assert sum(1 for c in cards if c.get("analysis", {}).get("writer") == "dry") == 10


# --- the console block ----------------------------------------------------------------------------

def texts_of(component) -> list[str]:
    out = []
    stack = [component]
    while stack:
        c = stack.pop()
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, (list, tuple)):
            stack.extend(c)
        elif c is not None:
            stack.append(getattr(c, "children", None))
    return out


def test_the_analysis_block_renders(demo):
    from dash import html

    from web.analysis_view import NEVER_VOICED, SOURCES, TITLE, WITHDRAWN, analysis_block

    sink, _ = demo
    card = next(c for c in sink.cards.values() if c["minute_label"] == "52'")
    block = analysis_block(card, "fr")
    words = texts_of(block)
    assert isinstance(block, html.Div) and TITLE in words and NEVER_VOICED in words and SOURCES in words and WITHDRAWN not in words
    assert card["analysis"]["texts"]["fr"][0]["line"] in words and card["analysis"]["texts"]["en"][0]["line"] not in words
    assert "Team threat share recent pct" in words or any("threat" in w.lower() for w in words)
    assert card["analysis"]["texts"]["en"][0]["line"] in texts_of(analysis_block(card, "de"))  # a missing language falls back to English
    assert "Written by template, verified, machine-translated" in texts_of(analysis_block(card, "es"))
    ruled_out = next(c for c in sink.cards.values() if c["minute_label"] == "63'")
    assert WITHDRAWN in texts_of(analysis_block(ruled_out, "en"))
    assert analysis_block({"id": "x"}, "en") is None and analysis_block(None) is None


def test_the_console_detail_shows_the_analyst_notes(demo):
    from web import console
    from web.analysis_view import TITLE

    sink, _ = demo
    card = next(c for c in sink.cards.values() if c["minute_label"] == "52'")
    assert TITLE in texts_of(console.detail(card, [], read_only=True, lang="en"))
    assert TITLE not in texts_of(console.detail({k: v for k, v in card.items() if k != "analysis"}, [], read_only=True, lang="en"))
