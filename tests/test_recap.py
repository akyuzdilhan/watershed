import inspect
import json
import re
import sys

import pytest

from generator import generate_match
from worker import recap as recap_cli
from worker.desk import Desk
from worker.pipeline import JsonFileSink, MemorySink, Pipeline
from worker.recap import (MAX_WORDS, RECAP_INSTRUCTIONS, RULED_OUT_WORDS, RecapTemplateWriter, RecapWriter, analyse, build_recap, recap_facts, season_facts,
                          verify_recap)
from worker.writer import EM_DASH, TemplateWriter, sentences, word_count


def run_desk(seed: int, story: str = "random", sink=None, frames: bool = False):
    stream, truth = generate_match(seed, story, frames=frames)
    sink = sink or MemorySink()
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True))
    for m in stream:
        pipeline.handle(m)
    pipeline.flush()
    return list(sink.cards.values()), next(iter(sink.states.values())), truth


@pytest.fixture(scope="module")
def demo():
    cards, state, truth = run_desk(7, "demo")
    return cards, state, truth, build_recap(cards, state)


def test_demo_recap_is_verified_in_three_languages(demo):
    _, _, _, recap = demo
    assert recap["final"] and recap["writer"] == "template" and recap["verification"]["verified"]
    assert sorted(t["lang"] for t in recap["texts"]) == ["en", "es", "fr"] and recap["machine_translated"] == ["es"]
    for t in recap["texts"]:
        for section, limit in MAX_WORDS.items():
            assert 0 < word_count(t[section]) <= limit  # "44 %" is one word
    assert recap["facts"]["final_score"] == "Rovers 5-2 United" and recap["facts"]["half_time_score"] == "Rovers 0-2 United"


def test_turning_point_is_a_second_half_rovers_goal(demo):
    cards, _, truth, recap = demo
    tp = recap["analysis"]["turning_point"]
    rovers_second_half = {f"card-{g['event_id']}" for g in truth["goals"] if g["counts"] and g["team"] == "home" and g["period"] == 2}
    assert tp["card_id"] in rovers_second_half and tp["player"] == "Dorian Lightfoot" and tp["swing_pts"] > 30
    assert recap["analysis"]["team_summary"]["home"]["scorers"][0] == {"name": "Dorian Lightfoot", "goals": 4, "minutes": ["52'", "57'", "66'", "83'"]}


def test_no_retracted_card_is_cited(demo):
    cards, state, truth, recap = demo
    status = {c["id"]: c["status"] for c in cards}
    retracted = [c for c in cards if c["status"] == "retracted"]
    assert len(retracted) == 2
    assert all(status[i] == "approved" for i in recap["cited_cards"] + recap["analysis"]["cards_on_air"])
    voided = {c["event_id"] for c in retracted}
    assert all(p["minute_label"] not in ("63'", "69'") for p in recap["analysis"]["wp_curve"])
    assert not voided & set(recap["analysis"]["cards_on_air"])
    # a retracted minute appears only as a goal ruled out, in a sentence that says so, from the disallowed_* facts
    assert {k for k, v in recap["facts"].items() if v in ("63'", "69'")} == {"disallowed_1_minute", "disallowed_2_minute"}
    for t in recap["texts"]:
        for section in MAX_WORDS:
            for s in sentences(t[section]):
                if "63'" in s or "69'" in s:
                    assert re.search(RULED_OUT_WORDS[t["lang"]], s), s


def test_recap_tells_the_goals_ruled_out_and_why(demo):
    _, _, _, recap = demo
    assert [(g["minute_label"], g["player"], g["reason"], g["margin_cm"]) for g in recap["analysis"]["disallowed_goals"]] == [
        ("63'", "Dorian Lightfoot", "offside", None), ("69'", "Vito Valtonen", "handball", None)]  # the desk keeps the margin off ruled_out: it is for the producer
    texts = {t["lang"]: t for t in recap["texts"]}
    assert "VAR ruled out Dorian Lightfoot's goal at 63' (offside) and Vito Valtonen's goal at 69' (handball)." in texts["en"]["recap"]
    assert "(hors-jeu)" in texts["fr"]["recap"] and "(main)" in texts["fr"]["recap"]
    assert "(fuera de juego)" in texts["es"]["recap"] and "(mano)" in texts["es"]["recap"]
    assert recap["verification"]["verified"]
    claims = {(c["source_field"], c["value"]) for t in recap["texts"] for c in t["claims"]}
    assert ("disallowed_2_minute", 69) in claims and not any("25" in t["recap"] for t in recap["texts"])


def test_disallowed_goals_can_be_passed_explicitly(demo):
    cards, state, _, _ = demo
    plain = [{k: v for k, v in c.items() if k not in ("ruled_out", "var_decision")} for c in cards]  # a desk written before the cards recorded VAR decisions
    assert build_recap(plain, state)["analysis"]["disallowed_goals"] == []
    given = [{"minute_label": "63'", "team": "home", "player": "Dorian Lightfoot", "reason": "offside", "margin_cm": 25}]
    recap = build_recap(plain, state, disallowed=given)
    en = next(t for t in recap["texts"] if t["lang"] == "en")
    assert "offside by a whisker" in en["recap"] and recap["verification"]["verified"]


def test_points_mean_only_a_change_in_win_probability(demo):
    _, _, _, recap = demo
    assert not [k for k in recap["facts"] if k.endswith("_pts") and "swing" not in k]  # value_pts orders the moments, it is never a fact to write
    for t in recap["texts"]:
        for section in MAX_WORDS:
            assert "pts" not in t[section]
            for m in re.finditer(r"(\d+) (?:points|puntos)", t[section]):
                assert int(m.group(1)) == recap["facts"]["turning_point_swing_pts"], t[section]


def test_only_approved_cards_count(demo):
    cards, state, _, _ = demo
    tp = analyse(cards, state)["turning_point"]["card_id"]
    others = [dict(c, status="rejected") if c["id"] == tp else c for c in cards]
    recap = build_recap(others, state)
    assert recap["analysis"]["turning_point"]["card_id"] != tp and tp not in recap["cited_cards"]
    assert "half_time_score" not in recap["facts"]  # the approved goals no longer add up to the final score
    assert recap["verification"]["verified"]


def test_fan_text_for_the_losing_side(demo):
    cards, state, _, _ = demo
    recap = build_recap(cards, state, fan_side="away")
    assert recap["fan_team"] == "United" and recap["verification"]["verified"]
    en = next(t for t in recap["texts"] if t["lang"] == "en")
    assert en["fan"].startswith("Hard to take for United")


def test_verifier_rejects_invented_numbers_names_and_retracted_moments(demo):
    cards, state, _, recap = demo
    facts = recap["facts"]
    good = [dict(t) for t in recap["texts"]]
    assert verify_recap(json.dumps(good), ["en", "fr", "es"], facts, cards)[1]["verified"]
    cases = {
        "invented number": "Rovers won with 61% possession.",
        "retracted goal": "Dorian Lightfoot scored at 63' too.",
        "unknown player": "Rovers won, Umberto Valtonen starred.",
        "spelled number": "Dorian Lightfoot scored four goals.",
        "too long": " ".join(["Rovers"] * (MAX_WORDS["recap"] + 1)),
    }
    for case, text in cases.items():
        bad = [dict(good[0], recap=text)] + good[1:]
        assert not verify_recap(json.dumps(bad), ["en", "fr", "es"], facts, cards)[1]["verified"], case
    assert not verify_recap(json.dumps(good[:2]), ["en", "fr", "es"], facts, cards)[1]["verified"]
    assert not verify_recap("not json", ["en"], facts, cards)[1]["verified"]


@pytest.mark.parametrize("seed", range(1, 13))
def test_unscripted_matches_get_a_verified_recap(seed):
    cards, state, _ = run_desk(seed)
    recap = build_recap(cards, state)
    assert recap["verification"]["verified"], recap["verification"]["report"]
    facts, _, roles = recap_facts(recap["analysis"])
    assert roles["fan_result"] in ("won", "draw")


class FailingWriter:
    name = "flaky-model"

    def __init__(self) -> None:
        self.calls = 0

    async def write_async(self, match_id, facts, languages, note=None):
        self.calls += 1
        return json.dumps([{"lang": lang, "recap": "Rovers won 9-0.", "fan": "Yes.", "analyst": "Yes.", "claims": []} for lang in languages])


def test_a_failing_model_gets_one_retry_then_the_template(demo):
    cards, state, _, _ = demo
    writer = FailingWriter()
    recap = build_recap(cards, state, writer)
    assert writer.calls == 2 and recap["writer"] == "template" and recap["verification"] == {**recap["verification"], "verified": True, "attempts": 2}


def test_cli_writes_recap_next_to_the_cards(tmp_path, monkeypatch, capsys):
    run_desk(7, "demo", sink=JsonFileSink(tmp_path))
    monkeypatch.setattr(sys, "argv", ["recap", str(tmp_path / "demo-0007")])
    recap_cli.main()
    written = json.loads((tmp_path / "demo-0007" / "recap.json").read_text(encoding="utf-8"))
    assert written["verification"]["verified"] and written["id"] == "recap-demo-0007"
    assert json.loads(capsys.readouterr().out)["id"] == "recap-demo-0007"


# --- the table after the match -------------------------------------------------------------------------

SEASON_FACTS = {"season_rovers_rank_before": 17, "season_rovers_relegation_chance_before_pct": 11, "season_rovers_relegation_chance_after_pct": 4}


@pytest.mark.parametrize("lang,matchday,provisional", [
    ("en", "Rovers move from 17th to 16th once the matchday is played", "With this result, Rovers are provisionally 14th (17th before the match)"),
    ("fr", "Après la journée, Rovers est 16e (17e avant le match)", "Avec ce résultat, Rovers est provisoirement 14e (17e avant le match)"),
    ("es", "Tras la jornada, el Rovers queda 16.º (17.º antes del partido)", "Con este resultado, el Rovers es provisionalmente 14.º (17.º antes del partido)"),
])
def test_the_season_sentence_gives_the_rank_after_the_matchday_or_says_it_is_provisional(lang, matchday, provisional):
    writer = RecapTemplateWriter({"season_slug": "rovers", "season_club": "Rovers"})
    for facts, expected in ((SEASON_FACTS | {"season_rovers_rank_after_matchday": 16}, matchday), (SEASON_FACTS | {"season_rovers_rank_after": 14}, provisional)):
        text, claims = writer.fill(writer.season_sentence(lang, facts), facts)
        assert text.replace("\xa0", " ").startswith(expected) and {c["source_field"] for c in claims} <= set(facts), text


def test_season_facts_keep_the_rank_after_the_matchday_and_no_league_points(monkeypatch):
    import worker.storyline as storyline

    extra = {"final_score": "Rovers 5-2 United"}
    for club, ranks, chances in (("rovers", (17, 14, 16), (11, 4)), ("united", (18, 18, 19), (34, 52))):
        extra |= {f"{club}_rank_before": ranks[0], f"{club}_rank_after": ranks[1], f"{club}_rank_after_matchday": ranks[2], f"{club}_points_before": 28, f"{club}_points_after": 31,
                  f"{club}_in_drop_zone_before": ranks[0] >= 18, f"{club}_in_drop_zone_after": ranks[1] >= 18, f"{club}_relegation_chance_before_pct": chances[0],
                  f"{club}_relegation_chance_after_pct": chances[1], f"{club}_relegation_change_pts": abs(chances[1] - chances[0]), f"{club}_relegation_change_direction": "down"}
    monkeypatch.setattr(storyline, "post_match", lambda story, home, away: (dict(extra), {k: k for k in extra}))
    facts, meanings, roles = {}, {}, {}
    analysis = {"score": {"home": 5, "away": 2}, "teams": {"home": "Rovers", "away": "United"}, "winner": "home"}
    season_facts({"focus": {"Rovers": "relegation", "United": "relegation"}}, analysis, facts, meanings, roles)
    assert facts["season_rovers_rank_after_matchday"] == 16 and "season_rovers_rank_after" not in facts and set(meanings) == set(facts)
    assert facts["season_rovers_in_drop_zone_after_matchday"] is False and facts["season_united_in_drop_zone_after_matchday"] is True
    assert not [k for k in facts if k.endswith("_in_drop_zone_after")] and not [k for k in facts if "points" in k.split("_")]
    assert roles == {"season_club": "Rovers", "season_slug": "rovers"}  # the winner's season: its chance of going down moved by 7 points


# --- texts and typography -----------------------------------------------------------------------------

def test_every_language_keeps_its_moments_sentence_and_its_typography(demo):
    _, _, _, recap = demo
    texts = {t["lang"]: t for t in recap["texts"]}
    for lang, words in (("en", "Top moments: "), ("fr", "Moments les plus lourds\xa0: "), ("es", "Momentos de más peso: ")):
        assert words in texts[lang]["analyst"], texts[lang]["analyst"]
    winner = recap["facts"]["winner"]  # in the demo the winner is also the fan team and the team of the turning point
    for section in MAX_WORDS:
        fr = texts["fr"][section]
        assert not re.search(r" [:;%?!]", fr) and not re.search(rf"\b(?:le|la|les|du|des|au|aux)\s+{winner}\b|\b[ld]['’]\s?{winner}\b", fr), fr  # no article before a club
    assert re.search(r"à la \d+'", texts["fr"]["recap"]), texts["fr"]["recap"]  # "à la 66'", not "à 66'"
    es = texts["es"]
    assert f"El {winner} cayó" in es["recap"] and f"llevó al {winner}" in es["recap"] and f"para el {winner}" in es["fan"], es  # the article before a club


# --- goals ruled out by VAR ---------------------------------------------------------------------------

def stored(cards, offside_call=None, keep_ruled_out=True):
    """The cards as a store that keeps no feed events gives them back: the desk could not find the margin; `var_decision` is what the worker put on the card."""
    out = []
    for c in json.loads(json.dumps(cards)):
        if c.get("ruled_out"):
            cause = c["ruled_out"]["cause"]
            c["var_decision"] = {"reason": cause, "call": offside_call if cause == "var_offside" else None, "margin_m": None}
            if keep_ruled_out:
                c["ruled_out"]["margin_cm"] = None
            else:
                c.pop("ruled_out")
        out.append(c)
    return out


@pytest.mark.parametrize("call,words", [("tight", "offside by a whisker"), ("clear", "clear offside")])
@pytest.mark.parametrize("keep_ruled_out", [True, False])
def test_the_offside_call_survives_a_store_without_feed_events(demo, call, words, keep_ruled_out):
    cards, state, _, _ = demo
    recap = build_recap(stored(cards, call, keep_ruled_out), state)
    en = next(t for t in recap["texts"] if t["lang"] == "en")
    assert [g["reason"] for g in recap["analysis"]["disallowed_goals"]] == ["offside", "handball"] and recap["verification"]["verified"]
    assert f"({words})" in en["recap"] and f"({words})" in en["analyst"] and "(handball)" in en["recap"]
    assert recap["facts"]["disallowed_1_offside_call"] == call and not any("margin" in k for k in recap["facts"])


def test_the_margin_in_var_decision_is_never_copied_into_the_recap(demo):
    """Cards whose desk keeps the margin out of `ruled_out` and whose worker puts it in `var_decision`: the recap document, stored next to the cards, gets the call and never the margin."""
    cards, state, _, _ = demo
    given = []
    for c in json.loads(json.dumps(cards)):
        if c.get("ruled_out"):
            margin = c["ruled_out"]["margin_cm"]
            c["var_decision"] = {"reason": c["ruled_out"]["cause"], "call": "tight" if margin is not None else None, "margin_m": margin / 100 if margin is not None else None}
            c["ruled_out"]["margin_cm"] = None
        given.append(c)
    assert any(c.get("var_decision", {}).get("margin_m") for c in given)
    recap = build_recap(given, state)
    assert [g["margin_cm"] for g in recap["analysis"]["disallowed_goals"]] == [None, None] and recap["verification"]["verified"]
    assert "margin_m" not in json.dumps(recap) and not any("margin" in k for k in recap["facts"])
    assert "(offside by a whisker)" in next(t for t in recap["texts"] if t["lang"] == "en")["recap"]


# --- the instructions and the model's replies ------------------------------------------------------------

def test_recap_instructions_follow_the_templates():
    assert "25 cm" not in RECAP_INSTRUCTIONS and "the margin when given" not in RECAP_INSTRUCTIONS and "80 words" not in RECAP_INSTRUCTIONS
    assert "offside by a whisker" in RECAP_INSTRUCTIONS and "clear offside" in RECAP_INSTRUCTIONS and "never give a margin" in RECAP_INSTRUCTIONS
    assert all(f"at most {limit} words" in RECAP_INSTRUCTIONS for limit in MAX_WORDS.values()) and not re.search(r"<\w+_words>", RECAP_INSTRUCTIONS)
    assert "provisionally 14th" in RECAP_INSTRUCTIONS and EM_DASH not in RECAP_INSTRUCTIONS
    assert inspect.signature(RecapWriter).parameters["max_tokens"].default == 3000


class ScriptedWriter:
    """Replays crafted recap replies and keeps the notes of the retries: no model call."""

    name = "scripted-model"

    def __init__(self, replies) -> None:
        self.replies, self.notes = list(replies), []

    async def write_async(self, match_id, facts, languages, note=None):
        self.notes.append(note)
        return self.replies.pop(0)


def test_model_replies_follow_the_on_air_rules(demo):
    cards, state, _, recap = demo
    facts, good = recap["facts"], [dict(t) for t in recap["texts"]]
    swing, winner = facts["turning_point_swing_pts"], facts["winner"]
    dashed = [dict(good[0], recap=good[0]["recap"].replace(": ", f" {EM_DASH} ", 1))] + good[1:]  # the dash becomes a comma
    texts, report = verify_recap(json.dumps(dashed, ensure_ascii=False), ["en", "fr", "es"], facts, cards)
    assert report["verified"] and EM_DASH not in texts[0]["recap"]
    for text in (f"{winner} take {swing} points from that goal.", "VAR ruled out a goal at 63': offside by 25 cm.", "About one match in 101 goes that way."):
        bad = [dict(good[0], recap=text)] + good[1:]
        assert not verify_recap(json.dumps(bad, ensure_ascii=False), ["en", "fr", "es"], facts, cards)[1]["verified"], text
    league = [dict(good[0], recap=f"{winner} take {swing} points from that goal.")] + good[1:]
    writer = ScriptedWriter([json.dumps(league, ensure_ascii=False), json.dumps(good, ensure_ascii=False)])
    out = build_recap(cards, state, writer)
    assert out["writer"] == "scripted-model" and out["verification"]["attempts"] == 2 and "league_points_verb" in writer.notes[1]
