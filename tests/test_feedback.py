import json

import pytest

from worker import feedback as fb
from worker.desk import Desk
from worker.writer import TemplateWriter, card_facts, writer_prompt

TEAMS = {"home": "Varleigh", "away": "Yarnford"}


def make_card(moment="goal", side="home", card_id=None):
    facts = {"goal": {"team_wp_before_pct": 30, "team_wp_after_pct": 54}, "save": {"xg_pct": 20, "team_wp_before_pct": 81, "team_wp_if_goal_pct": 24}}[moment]
    return {"id": card_id or f"c-{moment}-{side}", "version": 1, "match_id": "m", "team": side, "team_name": TEAMS[side], "moment": moment, "minute_label": "52'",
            "clock_s": 3000.0, "event_id": "e1", "player": {"id": f"{side}-9", "name": "Elio Ingleby"}, "score_before": {"home": 1, "away": 1}, "facts": facts, "value_pts": 24}


class Sink:
    def __init__(self):
        self.cards, self.audit, self.feedback_docs = {}, [], []

    def card(self, card):
        self.cards[card["id"]] = json.loads(json.dumps(card))

    def audit_entry(self, entry):
        self.audit.append(entry)

    def feedback(self, item):
        self.feedback_docs.append(item)


@pytest.fixture
def written():
    sink = Sink()
    desk = Desk(sink, TemplateWriter(), languages=("en", "fr"), profile={})
    card = desk.write(make_card(), TEAMS)
    return sink, desk, card


def vote(doc_id, verdict, text, part="why", lang="en", reasons=(), note=None, at=0.0, writer="template", family=None, moment="goal", by="p1", pattern=None, index=0):
    return {"id": f"feedback-{doc_id}-v1-{part}-{lang}-{index}-{by}", "type": "feedback", "match_id": "m", "doc_id": doc_id, "doc_type": "insight" if family else "card",
            "doc_version": 1, "moment": None if family else moment, "family": family, "target": {"part": part, "lang": lang, "index": index}, "text": text, "speaker": None,
            "writer": writer, "verdict": verdict, "reasons": list(reasons), "note": note, "by": by, "facts": {"minute": "52'"}, "field_meanings": {"minute": "match minute"},
            "pattern": pattern or text.lower(), "at": at}


# --- storage --------------------------------------------------------------------------------------

def test_feedback_is_stored_with_the_exact_text_and_audited(written):
    sink, desk, card = written
    en = next(t for t in card["texts"] if t["lang"] == "en")
    item = desk.feedback(card, {"part": "why", "lang": "en", "index": 0}, "down", ["too_long", "wrong_tone"], "  Shorter please  ", "producer-1", TEAMS)
    assert item["type"] == "feedback" and item["match_id"] == "m" and item["doc_id"] == card["id"] and item["doc_version"] == 1
    assert item["text"] == en["why"] and item["writer"] == "template" and item["moment"] == "goal"
    assert item["reasons"] == ["too_long", "wrong_tone"] and item["note"] == "Shorter please"
    assert item["facts"] == card_facts(card, TEAMS)[0] and item["field_meanings"]
    assert sink.feedback_docs == [item]
    entry = sink.audit[-1]
    assert entry["action"] == "feedback" and entry["by"] == "producer-1" and entry["verdict"] == "down" and entry["target"] == {"part": "why", "lang": "en", "index": 0}
    assert sink.cards[card["id"]]["history"] == card["history"]  # the card is not rewritten by a feedback
    up = desk.feedback(card, {"part": "duo", "lang": "fr", "index": 1}, "up", ["too_long"], None, "producer-1", TEAMS)
    fr_lines = [d for d in card["duo"] if d["lang"] == "fr"]
    assert up["text"] == fr_lines[1]["line"] and up["speaker"] == fr_lines[1]["speaker"] and up["reasons"] == []


def test_feedback_refuses_an_unknown_target_verdict_or_reason(written):
    _, desk, card = written
    with pytest.raises(ValueError):
        desk.feedback(card, {"part": "headline", "lang": "de", "index": 0}, "up", [], None, "p", TEAMS)
    with pytest.raises(ValueError):
        desk.feedback(card, {"part": "headline", "lang": "en", "index": 0}, "maybe", [], None, "p", TEAMS)
    with pytest.raises(ValueError):
        desk.feedback(card, {"part": "headline", "lang": "en", "index": 0}, "down", ["boring"], None, "p", TEAMS)
    with pytest.raises(ValueError):
        desk.feedback(card, {"part": "duo", "lang": "en", "index": 9}, "down", [], None, "p", TEAMS)


def test_a_second_click_replaces_the_first():
    first = vote("c1", "down", "A", at=1.0)
    second = dict(first, verdict="up", at=2.0)
    assert fb.latest([first, second]) == [second]
    assert fb.by_target([second, first])["c1|why|en|0"]["verdict"] == "up"


# --- profile --------------------------------------------------------------------------------------

def test_profile_rules_and_examples_are_bounded_most_frequent_first():
    votes = [vote(f"c{i}", "down", f"Long sentence {i} that goes on and on", reasons=["too_long"], at=float(i)) for i in range(20)]
    votes += [vote("c-tone", "down", "WHAT A GOAL!!!", reasons=["wrong_tone"], at=50.0)]
    votes += [vote(f"n{i}", "down", f"Note sentence {i}", note=f"note number {i}", at=60.0 + i) for i in range(10)]
    votes += [vote(f"u{i}", "up", f"Good sentence {i}", at=100.0 + i) for i in range(10)]
    p = fb.build_profile(votes, now=0)
    assert p["votes"] == 41 and len(p["rules"]) <= fb.MAX_RULES
    assert p["rules"][0]["source"] == "too_long" and p["rules"][0]["count"] == 20 and p["rules"][0]["rule"] == fb.RULES["too_long"]
    assert len(p["examples"]["rejected"]) <= 2 * fb.MAX_EXAMPLES and len(p["examples"]["approved"]) <= 2 * fb.MAX_EXAMPLES
    assert p["examples"]["approved"][0]["text"] == "Good sentence 9"  # most recent first
    block = fb.prompt_block(p, make_card(), ["en"])
    assert block and len(block) <= fb.PROMPT_MAX_CHARS
    assert fb.RULES["too_long"] in block and "Good sentence 9" in block and "control room" in block.lower()
    assert fb.prompt_block(fb.empty_profile()) is None


def test_the_writer_prompt_carries_the_house_rules():
    p = fb.build_profile([vote("c1", "down", "Varleigh are flying!", reasons=["wrong_tone"], note="No hype", at=1.0),
                          vote("c2", "up", "Ingleby levels it.", part="headline", at=2.0)], now=0)
    card = make_card()
    prompt = writer_prompt(card, ["en"], preferences=fb.prompt_block(p, card, ["en"]))
    assert prompt.startswith("Write the cards for this request:")
    assert "Control room preferences" in prompt and fb.RULES["wrong_tone"] in prompt and "No hype" in prompt
    assert '"Varleigh are flying!"' in prompt and '"Ingleby levels it."' in prompt
    assert writer_prompt(card, ["en"]) == f"Write the cards for this request: {json.dumps({'card_id': card['id'], 'languages': ['en']})}"


def test_the_desk_hands_the_preferences_to_a_model_writer():
    seen = {}

    class Fake:
        name = "fake-model"

        async def write_async(self, card, facts, languages, note=None, preferences=None):
            seen["preferences"] = preferences
            return "not json"  # the Verifier refuses it, the template takes over

    profile = fb.build_profile([vote("c1", "down", "Too much", reasons=["too_long"], at=1.0)], now=0)
    card = Desk(Sink(), Fake(), languages=("en",), profile=profile).write(make_card(), TEAMS)
    assert fb.RULES["too_long"] in seen["preferences"] and card["writer"] == "template"


def test_the_profile_file_is_reread_when_it_changes(tmp_path):
    path = tmp_path / "profile.json"
    src = fb.source(path)
    assert src.get()["votes"] == 0
    fb.save_profile(fb.build_profile([vote("c1", "up", "Fine", at=1.0)], now=0), path)
    assert src.get()["votes"] == 1


# --- ranking --------------------------------------------------------------------------------------

def test_a_family_with_a_low_approval_rate_drops_but_never_below_the_floor():
    downs = [vote(f"i{i}", "down", f"Comeback line {i}", family="comeback", at=float(i)) for i in range(30)]
    ups = [vote(f"s{i}", "up", f"Scorer line {i}", family="scorer", at=float(i)) for i in range(30)]
    few = [vote(f"h{i}", "down", f"Lead line {i}", family="ht-lead-lost", at=float(i)) for i in range(fb.MIN_VOTES - 1)]
    p = fb.build_profile(downs + ups + few, now=0)
    assert p["families"]["comeback"]["weight"] == fb.FLOOR  # 0 in 30: the floor
    val, w = fb.adjust_value(80, "comeback-home", p, 40)
    assert w == fb.FLOOR and val == round(80 * fb.FLOOR)
    assert fb.adjust_value(80, "scorer-home-20", p, 40)[0] > 80  # approved families rise a little
    assert fb.adjust_value(100, "scorer-home-20", p, 40)[0] == 100
    assert fb.adjust_value(80, "ht-lead-lost-home", p, 40) == (80, 1.0)  # not enough votes yet
    assert fb.adjust_value(80, "goals-total", p, 40) == (80, 1.0)  # no vote at all
    goal_downs = fb.build_profile([vote(f"g{i}", "down", f"Goal line {i}", family="scorer", at=float(i)) for i in range(30)], now=0)
    assert fb.adjust_value(45, "scorer-home-20", goal_downs, 40)[0] == 40  # a goal fact stays admitted
    assert fb.adjust_value(30, "scorer-home-20", goal_downs, 40)[0] < 30  # below the bar it was already out


def test_a_goal_or_red_card_moment_is_never_weighed_down():
    p = fb.build_profile([vote(f"g{i}", "down", f"G {i}", moment=m, at=float(i)) for i in range(10) for m in ("goal", "save")], now=0)
    assert fb.moment_weight(p, "goal") == 1.0 and fb.moment_weight(p, "save") == fb.FLOOR


def test_family_of_an_insight_key():
    assert fb.family_of("scorer-home-20-4") == "scorer"
    assert fb.family_of("comeback-home-2-3") == "comeback"
    assert fb.family_of("ht-lead-lost-away") == "ht-lead-lost"
    assert fb.family_of("stat-of-the-match") == "stat-of-the-match"


# --- template variants ----------------------------------------------------------------------------

def test_a_template_variant_with_repeated_thumbs_down_is_picked_last():
    card = make_card()
    facts, _ = card_facts(card, TEAMS)
    first = json.loads(TemplateWriter().write(card, facts, ["en"]))[0]
    pattern = fb.skeleton(first["why"], facts)
    votes = [vote(f"c{i}", "down", first["why"], pattern=pattern, at=float(i), by=f"p{i}") for i in range(fb.DEMOTE_AFTER)]
    p = fb.build_profile(votes, now=0)
    assert p["demoted"] == [pattern]
    again = json.loads(TemplateWriter(fb.demoted(p)).write(card, facts, ["en"]))[0]
    assert again["why"] != first["why"] and fb.skeleton(again["why"], facts) != pattern
    assert again["headline"] == first["headline"]  # only the turned-down part moves
    one = fb.build_profile(votes[:1], now=0)
    assert one["demoted"] == []  # one thumbs down is not enough
    model = fb.build_profile([dict(v, writer="gpt-model") for v in votes], now=0)
    assert model["demoted"] == []  # only template variants are demoted


# --- export and measure ---------------------------------------------------------------------------

def test_export_writes_judgements_and_pairs(tmp_path):
    votes = [vote("c1", "down", "Varleigh are flying!", reasons=["wrong_tone"], note="No hype", at=1.0, writer="gpt-model"),
             dict(vote("c1", "up", "Ingleby levels it at 52'.", at=2.0), id="feedback-c1-v2-why-en-0-p1", doc_version=2),
             vote("c2", "up", "Another", lang="fr", at=3.0)]
    out = fb.export(votes, tmp_path / "dataset.jsonl")
    assert out == {"judgements": 3, "pairs": 1, "out": str(tmp_path / "dataset.jsonl")}
    rows = [json.loads(line) for line in (tmp_path / "dataset.jsonl").read_text(encoding="utf-8").splitlines()]
    judgement = rows[0]
    assert judgement["record"] == "judgement" and "by" not in judgement
    assert set(judgement) >= {"facts", "field_meanings", "text", "verdict", "reasons", "note", "writer", "lang", "part"}
    pair = rows[-1]
    assert pair["record"] == "pair" and pair["chosen"] == "Ingleby levels it at 52'." and pair["rejected"] == "Varleigh are flying!"
    assert pair["rejected_reasons"] == ["wrong_tone"] and pair["facts"] and pair["lang"] == "en"


def test_cli_build_and_export_read_the_desk(tmp_path, written, capsys):
    sink, desk, card = written
    desk.feedback(card, {"part": "headline", "lang": "en", "index": 0}, "up", [], None, "p1", TEAMS)
    (tmp_path / "desk" / "m").mkdir(parents=True)
    (tmp_path / "desk" / "m" / "feedback.jsonl").write_text("\n".join(json.dumps(d) for d in sink.feedback_docs) + "\n", encoding="utf-8")
    assert fb.main(["build", "--desk-dir", str(tmp_path / "desk"), "--out", str(tmp_path / "profile.json")]) == 0
    assert fb.load_profile(tmp_path / "profile.json")["votes"] == 1
    assert fb.main(["export", "--desk-dir", str(tmp_path / "desk"), "--out", str(tmp_path / "dataset.jsonl")]) == 0
    assert '"judgements": 1' in capsys.readouterr().out


def test_stats_by_writer_and_language():
    s = fb.stats([vote("a", "up", "x"), vote("b", "down", "y"), vote("c", "up", "z", lang="fr", writer="gpt-model")])
    assert s["votes"] == 3 and s["rate"] == round(2 / 3, 3)
    assert {(b["writer"], b["lang"]): (b["up"], b["votes"]) for b in s["by"]} == {("template", "en"): (1, 2), ("gpt-model", "fr"): (1, 1)}
