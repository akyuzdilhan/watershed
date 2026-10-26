import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dash import html, no_update
from flask import Flask

from generator import generate_match
from web import console, matchfeed, overlay, panels, security, store
from web.app import app, route
from worker.desk import Desk
from worker.pipeline import JsonFileSink, Pipeline
from worker.writer import TemplateWriter

SRC = Path(__file__).resolve().parents[1] / "src"
MATCH = "demo-0007"


def run_desk(root: Path, auto_approve: bool = False, stop_at_first_retract: bool = False) -> None:
    stream, _ = generate_match(7, "demo")
    sink = JsonFileSink(root)
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=auto_approve))
    for m in stream:
        results = pipeline.handle(m)
        if stop_at_first_retract and any(r.get("action") == "retract" for r in results):
            return
    pipeline.flush()


@pytest.fixture(scope="module")
def demo_desk(tmp_path_factory):
    root = tmp_path_factory.mktemp("desk")
    run_desk(root)
    return root


@pytest.fixture
def desk(demo_desk, tmp_path, monkeypatch):
    """A fresh copy of the demo desk, served as the process store, outside public mode."""
    root = tmp_path / "desk"
    (root / MATCH).mkdir(parents=True)
    for name in ("cards.json", "state.json", "audit.jsonl"):
        (root / MATCH / name).write_bytes((demo_desk / MATCH / name).read_bytes())
    s = store.FileStore(root)
    s.cache_s = 0.0
    store.set_store(s)
    monkeypatch.setenv("PUBLIC_MODE", "0")
    yield s
    store.set_store(None)


def card_at(s: store.Store, minute: str) -> dict:
    return next(c for c in s.cards(MATCH) if c["minute_label"] == minute)


def audit_of(s: store.Store, card_id: str) -> list[tuple[str, str]]:
    return [(e["action"], e.get("by")) for e in s.audit(MATCH) if e.get("card_id") == card_id and e.get("by")]


def texts_in(component) -> str:
    """Every string in a Dash component tree."""
    if component is None:
        return ""
    if isinstance(component, (str, int, float)):
        return str(component)
    if isinstance(component, (list, tuple)):
        return " ".join(texts_in(c) for c in component)
    return texts_in(getattr(component, "children", None))


# --- store ----------------------------------------------------------------------------------------

def test_file_store_reads_the_desk_written_by_the_worker(desk):
    assert [m["match_id"] for m in desk.matches()] == [MATCH]
    cards = desk.cards(MATCH)
    assert len(cards) == 11
    assert [c["value_pts"] for c in cards] == sorted((c["value_pts"] for c in cards), reverse=True)
    state = desk.state(MATCH)
    assert state["teams"] == {"home": "Rovers", "away": "United"} and len(state["wp_timeline"]) > 1000
    assert any(e["action"] == "retracted" for e in desk.audit(MATCH))


def test_store_selection_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("DESK", raising=False)
    monkeypatch.delenv("COSMOS_ENDPOINT", raising=False)
    monkeypatch.setenv("DESK_DIR", str(tmp_path))
    assert isinstance(store.from_env(), store.FileStore)
    monkeypatch.setenv("COSMOS_ENDPOINT", "https://example.documents.azure.com")
    assert isinstance(store.from_env(), store.FileStore)  # an explicit DESK_DIR wins over a stray endpoint


def test_a_half_written_file_keeps_the_last_good_read(desk):
    before = desk.cards(MATCH)
    (desk.root / MATCH / "cards.json").write_text("[{\"id\": ", encoding="utf-8")
    assert desk.cards(MATCH) == before


# --- producer decisions ---------------------------------------------------------------------------

def test_approve_and_reject_go_through_the_desk_and_are_audited(desk):
    goal, other = card_at(desk, "66'"), card_at(desk, "14'")
    message, done = console.decide("approve", MATCH, goal["id"], "producer-1")
    assert done and "approved by Producer" in message and "producer-1" not in message  # the screen names the producer, the audit keeps who
    message, done = console.decide("reject", MATCH, other["id"], "producer-1")
    assert done
    on_disk = {c["id"]: c for c in json.loads((desk.root / MATCH / "cards.json").read_text(encoding="utf-8"))}
    assert on_disk[goal["id"]]["status"] == "approved" and on_disk[other["id"]]["status"] == "rejected"
    assert len(on_disk) == 11
    assert audit_of(desk, goal["id"])[-1] == ("approved", "producer-1")
    assert audit_of(desk, other["id"])[-1] == ("rejected", "producer-1")
    assert on_disk[goal["id"]]["history"][-1] == {"action": "approved", "by": "producer-1", "version": 1}


def test_a_retracted_card_cannot_go_on_air(desk):
    retracted = card_at(desk, "63'")
    message, done = console.decide("approve", MATCH, retracted["id"], "producer-1")
    assert not done and "retracted" in message
    assert card_at(desk, "63'")["status"] == "retracted"


def test_an_edit_with_an_invented_number_is_refused(desk):
    card = card_at(desk, "57'")
    message, done = console.decide("approve-edited", MATCH, card["id"], "producer-1", {"en": {"why": "Rovers go from 8% to 33% to win."}})
    assert not done and "33" in message and "Verifier" in message
    assert card_at(desk, "57'")["status"] == "pending"


def test_an_edit_that_keeps_the_facts_goes_on_air(desk):
    card = card_at(desk, "57'")
    headline = f"{card['player']['name'].split()[-1]} again!"
    message, done = console.decide("approve-edited", MATCH, card["id"], "producer-1", {"en": {"headline": headline, "why": "Rovers jump from 8% to 32% to win."}})
    assert done, message
    after = card_at(desk, "57'")
    en = next(t for t in after["texts"] if t["lang"] == "en")
    assert after["status"] == "approved" and en["headline"] == headline
    assert [a for a, _ in audit_of(desk, card["id"])][-2:] == ["edited", "approved"]


def test_decisions_need_a_producer(desk):
    card = card_at(desk, "66'")
    message, done = console.decide("approve", MATCH, card["id"], None)
    assert not done and "Sign in" in message
    assert card_at(desk, "66'")["status"] == "pending"


# --- console views --------------------------------------------------------------------------------

def test_refresh_lists_the_rundown_and_draws_the_curve(desk):
    options, value = console.match_options(None)
    assert value == MATCH and len(options) == 1
    out = console.refresh(MATCH, None, None)
    assert len(out) == console.REFRESH_OUTPUTS
    rundown, selected, detail, head, fig, score, approve_off, edit_off, reject_off, sig = out
    cards = {c["id"]: c for c in desk.cards(MATCH)}
    order = [cards[o["value"]] for o in rundown]
    waiting = [c for c in order if c["status"] == "pending"]
    assert len(rundown) == 11 and waiting and order[:len(waiting)] == waiting  # the cards waiting for a decision on top
    assert all(a["clock_s"] <= b["clock_s"] for group in (waiting, order[len(waiting):]) for a, b in zip(group, group[1:]))  # each group in match order
    assert selected == waiting[0]["id"] and waiting[0]["minute_label"] in texts_in(head)
    assert "Retracted" in texts_in([o["label"] for o in rundown])
    assert len(text_blocks(detail)) == 1 and "English" in texts_in(detail)  # one language at a time, English first
    assert [t.name for t in fig.data[:3]] == ["Rovers win", "Draw", "United win"]
    assert {t.name for t in fig.data[3:]} == {"Cards", "Retracted cards"}
    assert len(fig.layout.shapes) == 1  # half time
    state = desk.state(MATCH)
    assert f"{state['score']['home']}-{state['score']['away']}" in score and "FT" in score and state["minute_label"] not in score  # final whistle
    assert not approve_off and not edit_off and not reject_off
    again = console.refresh(MATCH, selected, sig)
    assert all(v is no_update for v in again)  # nothing changed: nothing is sent
    spanish = console.refresh(MATCH, selected, sig, lang="es")
    assert spanish[2] is not no_update and "Spanish" in texts_in(spanish[2]) and console.AI_WRITTEN in texts_in(spanish[2]) and "English" not in texts_in(spanish[2])


def test_the_second_half_continues_after_first_half_added_time(desk):
    points, x_of, breaks = console.axis(desk.state(MATCH)["wp_timeline"])
    assert len(breaks) == 1 and 45 < breaks[0] < 52
    assert all(a["x"] <= b["x"] for a, b in zip(points, points[1:]))


def test_edit_panel_holds_one_block_per_language(desk):
    fields, style = console.edit_panel(True, MATCH, card_at(desk, "57'")["id"])
    assert len(fields) == 3 and style == {}
    assert console.edit_panel(True, MATCH, card_at(desk, "63'")["id"]) == ([], {"display": "none"})


def text_blocks(view) -> list:
    return find(view, lambda c: getattr(c, "className", "") == "text-block")


def test_card_detail_traces_every_number_to_its_field_and_shows_the_verifier(desk):
    save, teams = card_at(desk, "72'"), desk.state(MATCH)["teams"]
    _, meanings = console.facts_of(save, teams)
    worth = [c for t in save["texts"] for c in t["claims"] if c["source_field"].endswith("_pts")]
    assert worth  # what the save was worth, in points, in at least one language
    view = console.detail(save, desk.audit(MATCH), [], teams, lang=None)
    text = texts_in(view)
    for c in worth:
        assert f"{c['value']:g} {console.field_label(c['source_field'])} {meanings[c['source_field']]}" in text  # the number, its field in words, what the field means
    blocks = text_blocks(view)
    assert len(blocks) == len(save["texts"])
    for block, t in zip(blocks, save["texts"]):
        shown = texts_in(block)
        assert "Verifier: passes" in shown and "every number claimed" in shown and "claims match the facts" in shown
        assert all(f"{c['value']:g} {console.field_label(c['source_field'])}" in shown for c in t["claims"])
        assert {c.title for c in find(block, lambda c: getattr(c, "className", "") == "fact-key")} == {c["source_field"] for c in t["claims"]}  # the key on hover
    assert "Facts" not in text  # the raw facts table is gone
    reps = save["verification"].get("repetition_warnings", 0)
    assert (f"{reps} repetition warning(s)" if reps else "no repetition warning") in text
    assert len(text_blocks(console.detail(save, [], [], teams))) == 1  # by default, English only
    edited = dict(save, texts=[dict(t, why=t["why"] + " 33%.") if t["lang"] == "en" else t for t in save["texts"]])  # a number no fact backs
    blocks = text_blocks(console.detail(edited, [], [], teams, lang=None))
    assert "Verifier: fails" in texts_in(blocks[0]) and "every number claimed: 33" in texts_in(blocks[0])
    assert all("Verifier: passes" in texts_in(b) for b in blocks[1:])


def test_a_scorer_correction_is_shown_in_the_console_and_on_the_overlay(desk):
    corrected = next(c for c in desk.cards(MATCH) if c.get("version", 1) >= 2)
    first = next(m for m in matchfeed.load(REPLAY_DIR / f"{MATCH}.jsonl.gz") if m["type"] == "action" and m["event_id"] == corrected["event_id"])  # the goal as first credited
    previous, now = first["player"]["name"], corrected["player"]["name"]
    assert previous != now and corrected.get("previous_player") == previous  # the desk records the scorer the feed first credited
    rewritten = {k: v for k, v in corrected.items() if k != "previous_player"}
    assert f"Scorer corrected: {previous} → {now}" in texts_in(console.detail(corrected, []))
    assert "Scorer corrected" not in texts_in(console.detail(rewritten, []))  # no field: nothing changes
    assert "Scorer corrected" not in texts_in(console.detail(dict(corrected, version=1), []))
    for lang, label in (("en", "Scorer corrected"), ("fr", "Buteur corrigé"), ("es", "Gol reasignado")):
        assert label in texts_in(overlay.lower_third({"kind": "card", "card": dict(corrected, status="approved"), "detail": None}, lang))
        assert label not in texts_in(overlay.lower_third({"kind": "card", "card": dict(rewritten, status="approved"), "detail": None}, lang))
    cards = json.loads((desk.root / MATCH / "cards.json").read_text(encoding="utf-8"))
    (desk.root / MATCH / "cards.json").write_text(json.dumps([dict(c, previous_player=previous) if c["id"] == rewritten["id"] else c for c in cards]), encoding="utf-8")
    assert console.decide("approve", MATCH, rewritten["id"], "producer-1")[1]
    assert "Buteur corrigé" in texts_in(overlay.render(f"?match={MATCH}&lang=fr"))


def test_a_var_decision_shows_its_reason_and_call_and_the_margin_only_to_a_producer(desk, monkeypatch):
    decision = {"reason": "var_offside", "call": "tight", "margin_m": 0.37}
    ruled = dict(card_at(desk, "63'"), var_decision=decision)
    seen_by_producer, seen_by_visitor = texts_in(console.detail(ruled, desk.audit(MATCH))), texts_in(console.detail(ruled, desk.audit(MATCH), read_only=True))
    assert "VAR decision: Offside, tight call" in seen_by_producer and "0.37 m" in seen_by_producer
    assert "VAR decision: Offside, tight call" in seen_by_visitor and "0.37" not in seen_by_visitor
    assert "VAR decision: Handball" in texts_in(console.detail(dict(ruled, var_decision={"reason": "var_handball", "call": None, "margin_m": None}), []))
    assert "VAR decision" not in texts_in(console.detail({k: v for k, v in card_at(desk, "63'").items() if k not in ("ruled_out", "var_decision")}, []))
    cards = json.loads((desk.root / MATCH / "cards.json").read_text(encoding="utf-8"))
    (desk.root / MATCH / "cards.json").write_text(json.dumps([dict(c, var_decision=decision) if c["id"] == ruled["id"] else c for c in cards]), encoding="utf-8")
    monkeypatch.setenv("PUBLIC_MODE", "1")
    with app.server.test_request_context("/"):
        out = console._refresh_cb(1, 0, MATCH, ruled["id"], "en", None)
        shown = texts_in(out[2]) + texts_in(out[3])
    assert "tight call" in shown and "0.37" not in shown
    monkeypatch.setenv("PUBLIC_MODE", "0")
    with app.server.test_request_context("/"):
        assert "0.37 m" in texts_in(console._refresh_cb(1, 0, MATCH, ruled["id"], "en", None)[3])
    head = console.card_head(ruled)
    folded = find(head, lambda c: getattr(c, "className", "") == "producer-details")
    assert len(folded) == 1 and folded[0].open is False and "0.37 m" in texts_in(folded[0])  # the producer's margin, closed by default
    assert "0.37" not in texts_in([c for c in head.children if c is not folded[0]]) and "VAR decision: Offside, tight call" in texts_in(head)
    assert not find(console.card_head(ruled, read_only=True), lambda c: getattr(c, "className", "") == "producer-details")
    for lang in overlay.LANGS:  # never on the overlay
        for item in ({"kind": "retraction", "card": dict(ruled, retracted_on_air=True), "detail": None}, {"kind": "card", "card": dict(ruled, status="approved"), "detail": None}):
            on_screen = texts_in(overlay.lower_third(item, lang))
            assert "0.37" not in on_screen and "37 cm" not in on_screen and "tight" not in on_screen


def test_a_note_being_typed_survives_the_ticks(desk):
    goal = card_at(desk, "66'")
    key = f"{goal['id']}|why|en|0"
    assert console.give_feedback(MATCH, key, "verdict", "down", "producer-1")[1]  # a thumbs down opens the note field
    first = dash_post(REFRESH, *refresh_body(goal["id"])).get_json()["response"]
    assert {"type": "fb-n", "p": "d", "k": key} in json_ids(first["c-detail"])
    path = desk.root / MATCH / "state.json"  # the match goes on: the clock moves, another card goes on air
    state = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(dict(state, last_seq=state["last_seq"] + 1, minute_label="90+12'", final=False)), encoding="utf-8")
    assert console.decide("approve", MATCH, card_at(desk, "14'")["id"], "producer-1")[1]
    second = dash_post(REFRESH, *refresh_body(goal["id"], first["c-sig"]["data"], 2)).get_json()["response"]
    assert "c-detail" not in second and "90+12'" in second["c-score"]["children"] and "c-rundown" in second  # the note field is not drawn again: the draft stays
    assert console.give_feedback(MATCH, key, "reason", "too_long", "producer-1")[1]  # a change on this card draws it again
    third = dash_post(REFRESH, *refresh_body(goal["id"], second["c-sig"]["data"], 3)).get_json()["response"]
    assert "c-detail" in third
    assert console.refresh(MATCH, goal["id"], third["c-sig"]["data"], rev=0) == (no_update,) * console.REFRESH_OUTPUTS  # nothing changed since: nothing sent
    french = dash_post(REFRESH, *refresh_body(goal["id"], third["c-sig"]["data"], 4, lang="fr")).get_json()["response"]
    assert "c-detail" in french and "c-head" in french  # another language: the detail is drawn again


# --- overlay --------------------------------------------------------------------------------------

def test_overlay_shows_the_latest_approved_card_in_the_chosen_language(desk):
    assert "ov-empty" in overlay.render("?lang=fr")[-1].className  # nothing approved yet
    console.decide("approve", MATCH, card_at(desk, "66'")["id"], "producer-1")
    console.decide("approve", MATCH, card_at(desk, "57'")["id"], "producer-1")
    shown = overlay.render(f"?match={MATCH}&lang=fr")
    goal = card_at(desk, "57'")
    fr_headline = next(t["headline"] for t in goal["texts"] if t["lang"] == "fr")  # the templates vary their wording within a match
    assert fr_headline.startswith(goal["player"]["name"]) and fr_headline in texts_in(shown) and "57'" in texts_in(shown)
    es = texts_in(overlay.render("?lang=es&clean=1"))
    assert "escrito por IA, sin revisar" in es and "traducción automática" not in es and "EN" not in es
    assert "aprobación automática" not in es  # approved by a producer
    console.decide("reject", MATCH, card_at(desk, "57'")["id"], "producer-1")  # taken off air: the previous card is back
    assert "66'" in texts_in(overlay.render("?lang=en"))


def test_overlay_shows_the_retraction_of_a_card_that_was_on_air(tmp_path, monkeypatch):
    run_desk(tmp_path, auto_approve=True, stop_at_first_retract=True)
    s = store.FileStore(tmp_path)
    s.cache_s = 0.0
    item = overlay.on_air(s.cards(MATCH), s.audit(MATCH))
    assert item["kind"] == "retraction" and item["card"]["minute_label"] == "63'"
    text = texts_in(overlay.render("?lang=en", store=s))
    assert "Goal ruled out (VAR, offside)" in text and "63'" in text and "withdrawn" not in text
    assert "But refusé (VAR, hors-jeu)" in texts_in(overlay.render("?lang=fr", store=s))
    assert "Gol anulado (VAR, fuera de juego)" in texts_in(overlay.render("?lang=es", store=s))


def test_a_retraction_audited_by_the_feed_holds_from_the_minute_of_the_ruling():
    goal = {"id": "card-g", "type": "card", "status": "retracted", "retracted_on_air": True, "moment": "goal", "minute_label": "63'", "clock_s": 3745.0, "team": "home",
            "ruled_out": {"reason": "offside"}, "texts": [{"lang": "en", "headline": "Goal headline", "why": "Goal why."}]}
    audit = [{"card_id": "card-g", "action": "written", "by": "template", "at": 100.0}, {"card_id": "card-g", "action": "approved", "by": "auto-approve", "at": 100.0},
             {"card_id": "card-g", "action": "hold", "by": "feed", "at": 100.2, "minute_label": "63'"},
             {"card_id": "card-g", "action": "retracted", "by": "feed", "at": 100.5, "minute_label": "64'", "was_on_air": True}]
    assert overlay.from_feed(audit[-1]) and not overlay.from_feed(audit[1])
    for entries in (audit, [{k: v for k, v in e.items() if not (k == "by" and v == "feed")} for e in audit]):  # the desk's trail, then an older one without `by`
        held = overlay.on_air([goal], entries, 3800.0, 2)
        assert held is not None and held["kind"] == "retraction"  # the ruling came in the 64th minute: the retraction holds from there
        assert overlay.on_air([goal], entries, 3840.0 + overlay.RETRACTION_HOLD_S + 1, 2) is None


def test_a_card_holds_the_overlay_while_its_facts_wait_on_the_detail_line():
    goal = {"id": "card-g", "type": "card", "status": "approved", "moment": "goal", "minute_label": "50'", "clock_s": 2950.0, "team": "home",
            "texts": [{"lang": "en", "headline": "Goal headline", "why": "Goal why."}]}
    fact = {"id": "insight-f", "type": "insight", "status": "approved", "minute_label": "50'", "clock_s": 2950.0, "texts": [{"lang": "en", "headline": "Fact headline", "why": "Fact why."}]}

    def entry(doc: dict, action: str, at: float, by: str = "auto-approve") -> dict:
        return {"card_id": doc["id"], "action": action, "by": by, "at": at}

    same_message = [entry(goal, "written", 100.0, "template"), entry(goal, "approved", 100.0), entry(fact, "written", 100.1, "template"), entry(fact, "approved", 100.1)]

    def shown(clock, period=2, docs=(goal, fact), audit=same_message) -> dict:
        return overlay.on_air(list(docs), audit, clock, period)

    item = shown(2960.0)
    assert item["kind"] == "card" and item["card"]["id"] == goal["id"] and item["detail"]["id"] == fact["id"]
    lower = texts_in(overlay.lower_third(item, "en"))
    assert "Goal headline" in lower and "The detail Fact headline" in lower
    assert shown(2950.0 + overlay.CARD_HOLD_S)["card"]["id"] == fact["id"]  # the card's time is up: the fact takes the screen
    assert shown(None)["card"]["id"] == fact["id"]  # no clock: the latest approval
    assert shown(3100.0, docs=(dict(goal, under_review=True), fact))["card"]["id"] == goal["id"]  # held while VAR checks it
    assert shown(2960.0, docs=(dict(goal, status="rejected"), fact))["card"]["id"] == fact["id"]  # taken off air: nothing to hold
    late = [entry(goal, "written", 100.0, "template"), entry(goal, "approved", 160.0, "producer-1"), entry(fact, "approved", 161.0, "producer-1")]
    assert shown(3030.0, audit=late)["card"]["id"] == goal["id"]  # approved a minute after the goal: held from then
    assert shown(3040.0, audit=late)["card"]["id"] == fact["id"]
    first_half = dict(goal, minute_label="45+2'", clock_s=2820.0)
    assert shown(2830.0, period=1, docs=(first_half, fact))["card"]["id"] == goal["id"]
    assert shown(2705.0, period=2, docs=(first_half, fact))["card"]["id"] == fact["id"]  # no hold across half-time
    retracted = dict(goal, status="retracted", retracted_on_air=True)
    taken_back = same_message + [{"card_id": goal["id"], "action": "retract", "minute_label": "51'"}, dict(entry(goal, "retracted", 160.0, "feed"), was_on_air=True),
                                 entry(fact, "approved", 160.5)]
    item = shown(3065.0, docs=(retracted, fact), audit=taken_back)
    assert item["kind"] == "retraction" and item["detail"] is None  # the retraction holds the screen the same way
    assert shown(3060.0 + overlay.RETRACTION_HOLD_S + 1, docs=(retracted, fact), audit=taken_back)["card"]["id"] == fact["id"]
    alone = taken_back[:-1]  # nothing approved after the retraction
    assert shown(3060.0 + overlay.RETRACTION_HOLD_S - 1, docs=(retracted, fact), audit=alone)["kind"] == "retraction"
    assert shown(3060.0 + overlay.RETRACTION_HOLD_S + 1, docs=(retracted, fact), audit=alone) is None  # its time is up: it leaves the screen by itself
    assert shown(3060.0 + overlay.RETRACTION_HOLD_S + 1, docs=(dict(retracted, under_review=True), fact), audit=alone) is None
    assert shown(None, docs=(retracted, fact), audit=alone)["kind"] == "retraction"  # no clock: nothing expires


def test_the_overlay_says_auto_approved_only_for_the_desk_s_own_approval():
    card = {"id": "card-a", "type": "card", "status": "approved", "moment": "save", "minute_label": "20'", "clock_s": 1200.0, "team": "away",
            "texts": [{"lang": "en", "headline": "Save headline", "why": "Save why."}]}
    auto = [{"card_id": "card-a", "action": "written", "by": "template", "at": 10.0}, {"card_id": "card-a", "action": "approved", "by": "auto-approve", "at": 10.0}]
    item = overlay.on_air([card], auto, 1205.0, 1)
    assert item["auto"]
    for lang, label in (("en", "auto-approved"), ("fr", "validation automatique"), ("es", "aprobación automática")):
        assert label in texts_in(overlay.lower_third(item, lang))
    again = auto + [{"card_id": "card-a", "action": "approved", "by": "producer-1", "at": 12.0}]  # a producer approved it since: the last approval counts
    assert not overlay.on_air([card], again, 1205.0, 1)["auto"]
    assert "auto-approved" not in texts_in(overlay.lower_third(overlay.on_air([card], again, 1205.0, 1), "en"))
    by_history = dict(card, history=[{"action": "approved", "by": "auto-approve", "version": 1}])
    assert overlay.on_air([by_history], [])["auto"] and not overlay.on_air([dict(card, history=[])], [])["auto"]  # no audit trail: the card's history


def test_overlay_labels_spanish_as_ai_written_and_shows_the_final_whistle():
    card = {"id": "card-b", "type": "card", "status": "approved", "moment": "goal", "minute_label": "70'", "clock_s": 4200.0, "team": "home", "machine_translated": ["es"],
            "texts": [{"lang": lang, "headline": f"Headline {lang}", "why": "Why."} for lang in ("en", "fr", "es")]}
    item = {"kind": "card", "card": card, "detail": None}
    for lang, label in (("en", "AI-written, not reviewed"), ("fr", "rédigé par IA, non relu"), ("es", "escrito por IA, sin revisar")):
        assert label in texts_in(overlay.lower_third(item, lang)) if lang == "es" else label not in texts_in(overlay.lower_third(item, lang))
    assert overlay.AI_WRITTEN_LABEL["en"] == "AI-written, not reviewed" and "translat" not in json.dumps(overlay.AI_WRITTEN_LABEL)
    live = {"teams": {"home": "Home", "away": "Away"}, "score": {"home": 3, "away": 1}, "minute_label": "90+4'", "clock_s": 5640.0, "final": False}
    final = dict(live, final=True)
    assert "90+4'" in texts_in(overlay.score_bug(live)) and texts_in(overlay.score_bug(final)).split()[-1] == "FT"
    fact = {"id": "insight-x", "type": "insight", "status": "approved", "minute_label": "90+4'", "clock_s": 5640.0, "texts": [{"lang": "en", "headline": "Fact", "why": "Why."}]}
    shown = texts_in(overlay.lower_third({"kind": "card", "card": fact, "detail": None}, "en", final))
    assert "FT 3-1" in shown and "90+4'" not in shown
    assert "90+4'" in texts_in(overlay.lower_third({"kind": "card", "card": fact, "detail": None}, "en", live))  # not over yet
    assert "70'" in texts_in(overlay.lower_third(item, "en", final)) and "FT" not in texts_in(overlay.lower_third(item, "en", final))  # a goal keeps its minute


def test_a_goal_ruled_out_says_why_on_the_overlay_without_the_margin():
    goal = {"id": "card-c", "type": "card", "status": "retracted", "retracted_on_air": True, "moment": "goal", "minute_label": "40'", "clock_s": 2400.0, "team": "home",
            "texts": [{"lang": lang, "headline": f"Headline {lang}", "why": "Why."} for lang in ("en", "fr", "es")]}
    expected = {"offside": ("Goal ruled out (VAR, offside)", "But refusé (VAR, hors-jeu)", "Gol anulado (VAR, fuera de juego)"),
                "handball": ("Goal ruled out (VAR, handball)", "But refusé (VAR, main)", "Gol anulado (VAR, mano)")}
    for reason, messages in expected.items():
        for card in (dict(goal, var_decision={"reason": f"var_{reason}", "call": "tight", "margin_m": 0.12}), dict(goal, ruled_out={"reason": reason, "cause": f"var_{reason}", "margin_cm": 12})):
            for lang, message in zip(overlay.LANGS, messages):
                shown = texts_in(overlay.lower_third({"kind": "retraction", "card": card, "detail": None}, lang))
                assert message in shown and "0.12" not in shown and "12 cm" not in shown and "tight" not in shown, (reason, lang)
                assert f"Headline {lang}" in shown  # the headline taken back, struck through
    assert overlay.retraction_message(goal, "en") == "Goal ruled out"  # no VAR decision recorded
    assert overlay.retraction_message(dict(goal, var_decision={"reason": "var_other"}), "fr") == "But refusé (VAR)"
    assert overlay.retraction_message(dict(goal, moment="save"), "es") == "Información retirada"


def test_overlay_keeps_the_goal_on_screen_when_its_facts_are_approved_in_the_same_message(tmp_path):
    from test_insights import FakeHistory  # the small league history of the insight tests

    stream, _ = generate_match(7, "demo")
    sink = JsonFileSink(tmp_path)
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True), history=FakeHistory())
    s = store.FileStore(tmp_path)
    s.cache_s = 0.0
    messages = iter(stream)

    def until(done) -> list[dict]:
        for m in messages:
            results = pipeline.handle(m)
            if done(results):
                return results
        raise AssertionError("never happened")

    def goal_at(minute: str) -> dict:
        return next(r for r in until(lambda rs: any(r.get("moment") == "goal" and r["minute_label"] == minute for r in rs)) if r.get("moment") == "goal")

    def shown() -> tuple[dict, str]:
        state = s.state(MATCH)
        item = overlay.on_air(s.cards(MATCH) + overlay.aireable_insights(s.insights(MATCH)), s.audit(MATCH), state["clock_s"], state["period"])
        return item, texts_in(overlay.render(f"?match={MATCH}&lang=en", store=s))

    def facts_after(card_id: str) -> list[str]:
        audit = s.audit(MATCH)
        last = max(i for i, e in enumerate(audit) if e.get("card_id") == card_id and e.get("action") == "approved")
        return [e["card_id"] for e in audit[last + 1:] if e.get("action") == "approved"]

    for minute in ("57'", "63'", "66'", "83'"):
        goal = goal_at(minute)
        facts = facts_after(goal["id"])
        assert facts, minute  # the card and its facts went on air in the same message
        item, text = shown()
        headline = next(t["headline"] for t in s.card(MATCH, goal["id"])["texts"] if t["lang"] == "en")
        assert item["card"]["id"] == goal["id"] and item["detail"]["id"] == facts[-1] and headline in text and "The detail" in text, minute
        assert item["auto"] and "auto-approved" in text, minute  # the desk approved it itself
        if minute == "57'":  # the card's time is up: the latest fact takes the screen
            until(lambda rs: s.state(MATCH)["clock_s"] >= goal["clock_s"] + overlay.CARD_HOLD_S)
            item, text = shown()
            assert item["card"]["id"] == facts[-1] and headline not in text
        if minute == "63'":  # ruled out after VAR: the retraction is on screen, not a fact
            until(lambda rs: any(r.get("action") == "retract" for r in rs))
            item, text = shown()
            assert item["kind"] == "retraction" and item["card"]["id"] == goal["id"] and "Goal ruled out (VAR, offside)" in text
    for m in messages:  # on to the final whistle
        pipeline.handle(m)
    pipeline.flush()
    state = s.state(MATCH)
    item, text = shown()
    assert state["final"] and item["card"]["type"] == "insight" and item["auto"] and "auto-approved" in text
    assert f"FT {state['score']['home']}-{state['score']['away']}" in text and state["minute_label"] not in text  # the final score instead of the minute


def test_overlay_module_never_loads_the_writer_or_a_model():
    code = "import sys, web.overlay; print(json.dumps(sorted(m for m in sys.modules if m.startswith(('worker', 'agent_framework', 'openai', 'azure.ai')))))"
    out = subprocess.run([sys.executable, "-c", "import json; " + code], capture_output=True, text=True, check=True,
                         env={**os.environ, "PYTHONPATH": str(SRC)}).stdout
    loaded = json.loads(out)
    assert "worker.writer" not in loaded and "worker.desk" not in loaded
    assert not [m for m in loaded if m.startswith(("agent_framework", "openai", "azure.ai"))]
    source = (SRC / "web" / "overlay.py").read_text(encoding="utf-8")
    assert ".approve(" not in source and ".reject(" not in source and "worker" not in source


# --- public safety --------------------------------------------------------------------------------

def find(component, test) -> list:
    """Every component of a Dash tree for which `test` is true."""
    if isinstance(component, (list, tuple)):
        return [c for item in component for c in find(item, test)]
    if not hasattr(component, "to_plotly_json"):
        return []
    return ([component] if test(component) else []) + find(getattr(component, "children", None), test)


def test_public_mode_shows_visitors_a_read_only_console(desk, monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    monkeypatch.setenv("EASY_AUTH_ENABLED", "true")
    with app.server.test_request_context("/"):
        assert security.producer() is None
        page = route("/")
        assert "read-only" in page.className and "Read-only view" in texts_in(page) and "Signed in as" not in texts_in(page)
        assert {"c-pane-desk", "c-pane-graphics", "c-pane-insights", "c-detail", "c-wp", "c-i-detail", "c-g-figure"} <= component_ids(page)  # the whole console
        assert [a.href for a in find(page, lambda c: getattr(c, "href", None) and "sign-in" in texts_in(c))] == [security.LOGIN_URL]
        assert find(page, lambda c: "c-approve" in component_ids(getattr(c, "children", None)) and getattr(c, "className", "") == "actions")[0].style == {"display": "none"}
        assert "Read-only view" not in texts_in(route("/overlay"))  # the overlay stays as it was
    with app.server.test_request_context("/", headers={security.PRINCIPAL_HEADER: "ana@example.com"}):
        assert security.producer() == "ana@example.com"
        page = route("/")
        assert "Signed in as Producer" in texts_in(page) and "Read-only view" not in texts_in(page) and "read-only" not in page.className
        assert "ana@example.com" not in texts_in(page)  # the identity stays in the audit, never on screen
        monkeypatch.setenv("PRODUCER_DISPLAY_NAME", "Match desk")
        assert "Signed in as Match desk" in texts_in(route("/")) and "ana@example.com" not in texts_in(route("/"))
        monkeypatch.delenv("PRODUCER_DISPLAY_NAME")
    monkeypatch.setenv("PRODUCERS", "bob@example.com")
    with app.server.test_request_context("/", headers={security.PRINCIPAL_HEADER: "ana@example.com"}):
        assert security.producer() is None and "Read-only view" in texts_in(route("/"))
    monkeypatch.delenv("PRODUCERS")
    monkeypatch.setenv("EASY_AUTH_ENABLED", "false")  # without Easy Auth the header is forgeable
    with app.server.test_request_context("/", headers={security.PRINCIPAL_HEADER: "ana@example.com"}):
        assert security.producer() is None
        page = route("/")
        assert "Read-only view" in texts_in(page) and "Producer sign-in" not in texts_in(page)  # no sign-in to offer


def test_approve_callback_refuses_without_sign_in_in_public_mode(desk, monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    card = card_at(desk, "66'")
    pattern = [{"id": {"type": t, "lang": "en"}, "property": p, "value": v} for t, p, v in ()]
    body = {
        "output": "..c-message.children...c-rev.data..",
        "outputs": [{"id": "c-message", "property": "children"}, {"id": "c-rev", "property": "data"}],
        "inputs": [{"id": "c-approve", "property": "n_clicks", "value": 1}, {"id": "c-reject", "property": "n_clicks", "value": None},
                   {"id": "c-approve-edit", "property": "n_clicks", "value": None}],
        "state": [{"id": "c-match", "property": "value", "value": MATCH}, {"id": "c-rundown", "property": "value", "value": card["id"]},
                  {"id": "c-rev", "property": "data", "value": 0}, pattern, pattern, pattern, pattern],
        "changedPropIds": ["c-approve.n_clicks"],
    }
    response = app.server.test_client().post("/_dash-update-component", json=body, headers={security.PRINCIPAL_HEADER: "mallory"})
    assert response.status_code == 200
    assert "Sign in" in response.get_data(as_text=True)
    assert card_at(desk, "66'")["status"] == "pending"
    monkeypatch.setenv("PUBLIC_MODE", "0")
    response = app.server.test_client().post("/_dash-update-component", json=body)
    assert "approved by Producer" in response.get_data(as_text=True) and "local-producer" not in response.get_data(as_text=True)
    assert audit_of(desk, card["id"])[-1] == ("approved", "local-producer")
    assert card_at(desk, "66'")["status"] == "approved"


def test_rate_limiter_counts_per_ip_over_a_sliding_minute():
    limiter = security.RateLimiter(per_minute=3)
    assert all(limiter.allow("1.2.3.4", now=t) for t in (0.0, 1.0, 2.0))
    assert not limiter.allow("1.2.3.4", now=3.0)
    assert limiter.allow("5.6.7.8", now=3.0)
    assert limiter.allow("1.2.3.4", now=60.5)


def test_rate_limit_answers_429_on_pages_and_callbacks_only():
    server = Flask("t")
    server.add_url_rule("/", "index", lambda: "ok")
    server.add_url_rule("/assets/x.css", "asset", lambda: "css")
    security.install(server, per_minute=2)
    client = server.test_client()
    assert [client.get("/").status_code for _ in range(3)] == [200, 200, 429]
    assert client.get("/assets/x.css").status_code == 200
    other = {"X-Forwarded-For": "9.9.9.9, 10.0.0.1"}
    assert client.get("/", headers=other).status_code == 200  # the address the ingress appended
    spoofed = {"X-Forwarded-For": "1.1.1.1, 127.0.0.1"}
    assert client.get("/", headers=spoofed).status_code == 429  # a forged first entry does not reset the count


def test_rate_limit_lets_the_desk_the_graphics_and_the_overlay_run_together(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_PER_MIN", raising=False)
    assert security.install(Flask("default")).per_minute == 600
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "50")
    assert security.install(Flask("set")).per_minute == 50
    limiter = security.RateLimiter()
    # one address, one minute: the desk ticks every 0.75 s (two callbacks a 1.5 s tick), the graphics panel every 5 s, the overlay every second
    times = sorted([0.75 * i for i in range(80)] + [5.0 * i for i in range(12)] + [1.0 * i for i in range(60)])
    assert len(times) > 120 and all(limiter.allow("1.2.3.4", now=t) for t in times)


def dash_post(output: str, inputs: list, state: list, changed: list[str], headers: dict | None = None):
    """POSTs one callback to the Dash endpoint, as the browser does; `output` picks the callback by a part of its outputs."""
    key = next(k for k in app.callback_map if output in k)
    outputs = [{"id": part.split(".")[0], "property": part.split(".", 1)[1]} for part in key.strip(".").split("...")]
    body = {"output": key, "outputs": outputs, "inputs": inputs, "state": state, "changedPropIds": changed}
    return app.server.test_client().post("/_dash-update-component", json=body, headers=headers or {})


def json_ids(tree) -> list:
    """Every component id in a serialized Dash tree (a callback response)."""
    if isinstance(tree, list):
        return [i for t in tree for i in json_ids(t)]
    if not isinstance(tree, dict):
        return []
    if "namespace" not in tree:
        return [i for v in tree.values() for i in json_ids(v)]
    props = tree.get("props") or {}
    return ([props["id"]] if "id" in props else []) + json_ids(props.get("children"))


REFRESH = "c-detail.children"
DECIDE = "..c-message.children...c-rev.data.."
VISITOR = {"X-Forwarded-For": "203.0.113.7"}


def refresh_body(card_id: str, sig=None, n: int = 1, lang: str = "en") -> tuple[list, list, list]:
    inputs = [{"id": "c-tick", "property": "n_intervals", "value": n}, {"id": "c-rev", "property": "data", "value": 0},
              {"id": "c-match", "property": "value", "value": MATCH}, {"id": "c-rundown", "property": "value", "value": card_id},
              {"id": "c-lang", "property": "value", "value": lang}]
    return inputs, [{"id": "c-sig", "property": "data", "value": sig}], ["c-tick.n_intervals"]


def test_visitors_read_the_desk_without_producer_controls_or_data(desk, monkeypatch):
    goal = card_at(desk, "66'")
    assert console.decide("approve", MATCH, goal["id"], "ana@example.com")[1]
    assert console.give_feedback(MATCH, f"{goal['id']}|headline|en|0", "verdict", "down", "ana@example.com")[1]
    monkeypatch.setenv("PUBLIC_MODE", "1")
    monkeypatch.setenv("EASY_AUTH_ENABLED", "true")
    with app.server.test_request_context("/"):
        rundown, _, detail, head, fig, score, approve_off, edit_off, reject_off, _ = console._refresh_cb(1, 0, MATCH, goal["id"], "en", None)
    text = texts_in(detail) + texts_in(head)
    en = next(t for t in goal["texts"] if t["lang"] == "en")
    assert len(rundown) == 11 and en["headline"] in text and "5-2" in score and len(fig.data) >= 3
    assert approve_off and edit_off and reject_off
    assert not feedback_ids(detail) and not feedback_ids(detail, "fb-r") and not feedback_ids(detail, "fb-n")  # no thumbs, reasons or notes
    history = texts_in(find(detail, lambda c: getattr(c, "className", "") == "history"))
    assert "ana@example.com" not in text and "by a producer" in history and "feedback" not in history and "Producer feedback" not in text
    # the same through the Dash endpoint, as the visitor's browser asks for it
    options = dash_post("c-match.options", [{"id": "c-tick", "property": "n_intervals", "value": 1}], [{"id": "c-match", "property": "value", "value": None}],
                        ["c-tick.n_intervals"], VISITOR).get_json()["response"]
    assert options["c-match"]["value"] == MATCH
    response = dash_post(REFRESH, *refresh_body(goal["id"]), headers=VISITOR)
    assert response.status_code == 200
    payload = response.get_json()["response"]
    assert en["headline"] in json.dumps(payload["c-detail"], ensure_ascii=False) and payload["c-approve"]["disabled"] is True
    assert not [i for i in json_ids(payload["c-detail"]) if isinstance(i, dict) and i.get("type", "").startswith("fb")]
    monkeypatch.setenv("PUBLIC_MODE", "0")  # the producer, locally: every control
    with app.server.test_request_context("/"):
        detail = console._refresh_cb(1, 0, MATCH, goal["id"], "en", None)[2]
    history = texts_in(find(detail, lambda c: getattr(c, "className", "") == "history"))
    assert feedback_ids(detail) and feedback_ids(detail, "fb-n") and "by Producer" in history and "ana@example.com" not in texts_in(detail)
    assert audit_of(desk, goal["id"])[-2:] == [("approved", "ana@example.com"), ("feedback", "ana@example.com")]  # the audit keeps the identity


def test_visitors_cannot_decide_edit_or_judge_and_a_producer_can(desk, monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    monkeypatch.setenv("EASY_AUTH_ENABLED", "true")
    goal, other = card_at(desk, "66'"), card_at(desk, "57'")
    folder = desk.root / MATCH
    before = {name: (folder / name).read_bytes() for name in ("cards.json", "audit.jsonl")}
    hid = {"type": "c-edit-headline", "lang": "en"}

    def decision(button: str, card: dict, headline: str | None = None) -> tuple[list, list, list]:
        edits = [[{"id": hid, "property": "id", "value": hid}], [{"id": hid, "property": "value", "value": headline}], [], []] if headline else [[], [], [], []]
        inputs = [{"id": b, "property": "n_clicks", "value": 1 if b == button else None} for b in ("c-approve", "c-reject", "c-approve-edit")]
        state = [{"id": "c-match", "property": "value", "value": MATCH}, {"id": "c-rundown", "property": "value", "value": card["id"]},
                 {"id": "c-rev", "property": "data", "value": 0}, *edits]
        return inputs, state, [f"{button}.n_clicks"]

    def judge(kind: str, card: dict, value) -> tuple[list, list, list]:
        key = f"{card['id']}|why|en|0"
        cid = {"type": "fb", "p": "d", "k": key, "v": value} if kind == "fb" else {"type": "fb-r", "p": "d", "k": key, "r": value} if kind == "fb-r" else {"type": "fb-n", "p": "d", "k": key}
        prop, val = ("value", value) if kind == "fb-n" else ("n_clicks", 1)
        inputs = [[{"id": cid, "property": prop, "value": val}] if kind == k else [] for k in ("fb", "fb-r", "fb-n")]
        state = [{"id": "c-match", "property": "value", "value": MATCH}, {"id": "c-i-rev", "property": "data", "value": 0}]
        return inputs, state, [json.dumps(cid, sort_keys=True, separators=(",", ":")) + "." + prop]

    def edit_open(card: dict) -> tuple[list, list, list]:
        inputs = [{"id": "c-edit", "property": "n_clicks", "value": 1}, {"id": "c-cancel-edit", "property": "n_clicks", "value": None},
                  {"id": "c-rev", "property": "data", "value": 0}, {"id": "c-rundown", "property": "value", "value": card["id"]}]
        return inputs, [{"id": "c-match", "property": "value", "value": MATCH}], ["c-edit.n_clicks"]

    for headers in (VISITOR, {**VISITOR, security.PRINCIPAL_HEADER: ""}):  # nobody signed in
        for body in (decision("c-approve", goal), decision("c-reject", other), decision("c-approve-edit", goal, "Edited by a visitor")):
            assert "Sign in to approve or reject cards" in dash_post(DECIDE, *body, headers=headers).get_data(as_text=True)
        for body in (judge("fb", goal, "down"), judge("fb-r", goal, "too_long"), judge("fb-n", goal, "Too long by half")):
            assert "Sign in to give feedback" in dash_post("c-i-rev.data@", *body, headers=headers).get_data(as_text=True)
        panel = dash_post("c-edit-panel.children", *edit_open(goal), headers=headers).get_json()["response"]
        assert panel["c-edit-panel"]["children"] == [] and panel["c-edit-actions"]["style"] == {"display": "none"}
    assert {name: (folder / name).read_bytes() for name in before} == before and not (folder / "feedback.jsonl").exists()  # nothing written

    producer = {**VISITOR, security.PRINCIPAL_HEADER: "ana@example.com"}
    panel = dash_post("c-edit-panel.children", *edit_open(goal), headers=producer).get_json()["response"]
    assert len(panel["c-edit-panel"]["children"]) == len(goal["texts"])
    approved, rejected = (dash_post(DECIDE, *decision(b, c), headers=producer).get_data(as_text=True) for b, c in (("c-approve", goal), ("c-reject", other)))
    judged = dash_post("c-i-rev.data@", *judge("fb-r", goal, "too_long"), headers=producer).get_data(as_text=True)
    assert "approved by Producer" in approved and "Rejected by Producer" in rejected and "not relevant (too long), by Producer" in judged
    assert not any("ana@example.com" in answer for answer in (approved, rejected, judged))
    assert audit_of(desk, goal["id"])[-1] == ("feedback", "ana@example.com") and audit_of(desk, other["id"])[-1] == ("rejected", "ana@example.com")
    assert "with a note" in dash_post("c-i-rev.data@", *judge("fb-n", goal, "Too long by half"), headers=producer).get_data(as_text=True)
    assert card_at(desk, "66'")["status"] == "approved" and card_at(desk, "57'")["status"] == "rejected"
    latest = console.FB.by_target(desk.feedback_docs(MATCH))[f"{goal['id']}|why|en|0"]
    assert latest["reasons"] == ["too_long"] and latest["note"] == "Too long by half" and latest["by"] == "ana@example.com"


def test_public_console_never_loads_a_model(desk):
    code = ("import json, sys\n"
            "from web.app import app, route\n"
            "from web import console\n"
            "with app.server.test_request_context('/'):\n"
            "    page = route('/')\n"
            "    out = console._refresh_cb(1, 0, sys.argv[1], None, 'en', None)\n"
            "print(json.dumps({'read_only': 'read-only' in page.className, 'rundown': len(out[0]),\n"
            "                  'loaded': sorted(m for m in sys.modules if m.startswith(('agent_framework', 'openai', 'azure.ai')))}))\n")
    env = {k: v for k, v in os.environ.items() if k not in ("COSMOS_ENDPOINT", "DESK")}
    env.update(PYTHONPATH=str(SRC), PUBLIC_MODE="1", EASY_AUTH_ENABLED="true", DESK_DIR=str(desk.root))
    out = subprocess.run([sys.executable, "-c", code, MATCH], capture_output=True, text=True, check=True, env=env).stdout
    result = json.loads(out.strip().splitlines()[-1])
    assert result == {"read_only": True, "rundown": 11, "loaded": []}


# --- match graphics, insights and storyline -------------------------------------------------------

REPLAY_DIR = Path(__file__).resolve().parents[1] / "data" / "replay"


@pytest.fixture
def feed_desk(desk):
    desk.replay_dir = REPLAY_DIR
    return desk


def component_ids(component) -> set:
    out = set()
    if isinstance(component, (list, tuple)):
        for c in component:
            out |= component_ids(c)
        return out
    if hasattr(component, "to_plotly_json"):
        if getattr(component, "id", None):
            out.add(component.id)
        out |= component_ids(getattr(component, "children", None))
    return out


def write_docs(s: store.FileStore, with_texts: bool = True) -> list[dict]:
    from worker.tactics import insight_candidates

    feed = s.feed(MATCH)
    docs = insight_candidates(feed["actions"], feed["info"], MATCH)
    for d in docs:
        d["status"] = "pending"
        if with_texts:
            d["texts"] = [{"lang": "en", "headline": f"Detail {d['key']}", "why": "Checked against the frames.", "claims": []}]
            d["duo"] = [{"speaker": "Ada", "lang": "en", "line": "Look at that run!", "claims": []}, {"speaker": "Bram", "lang": "en", "line": "A quarter of a metre.", "claims": []}]
    (s.root / MATCH / "insights.json").write_text(json.dumps(docs), encoding="utf-8")
    (s.root / MATCH / "storyline.json").write_text(json.dumps({"id": f"storyline-{MATCH}", "type": "storyline", "match_id": MATCH, "status": "pending",
                                                               "facts": {"home_position": 4}, "field_meanings": {"home_position": "Rovers table position"},
                                                               "texts": [{"lang": "en", "headline": "Fourth against sixth", "why": "A top-four duel."}]}), encoding="utf-8")
    return docs


def test_console_layout_has_the_view_switch_and_the_new_panels():
    ids = component_ids(console.layout("producer-1"))
    assert {"c-view", "c-pane-desk", "c-pane-graphics", "c-pane-insights", "c-g-figure", "c-i-list", "c-s-detail", "c-g-tick", "c-i-tick"} <= ids
    desk_style, graphics_style, insights_style, g_off, i_off = panels.view_styles("graphics")
    assert graphics_style == {} and desk_style == insights_style == {"display": "none"} and (g_off, i_off) == (False, True)
    assert panels.view_styles(None)[3:] == (True, True)  # a hidden panel sends no request


def test_graphics_tab_draws_every_kind_from_the_feed(feed_desk):
    for kind in [k["value"] for k in panels.KINDS]:
        options, value, fig, note, sig = panels.graphics(MATCH, kind, None, "graphics", None, feed_desk)
        assert sig and note and len(fig.data) >= 1, kind
        if kind not in ("shots", "momentum"):
            assert options and value == (options[-1]["value"] if kind == "buildups" else options[0]["value"]), kind
        assert panels.graphics(MATCH, kind, value if value is not no_update else None, "graphics", sig, feed_desk)[2] is no_update  # unchanged: nothing re-sent
    _, _, fig, _, _ = panels.graphics(MATCH, "momentum", None, "graphics", None, feed_desk)
    assert len(fig.data) == 5  # threat bars and the desk's win-probability curve
    _, _, fig, note, _ = panels.graphics(MATCH, "tactics", "run:demo-0007-v2-a01178:home-20", "graphics", None, feed_desk)
    assert "margin_m" not in fig.layout.meta["offside"] and fig.layout.meta["offside"]["line_x"]  # the figure never carries the margin
    folded = find(note, lambda c: isinstance(c, html.Details))
    assert len(folded) == 1 and folded[0].open is False and "0.25 m beyond the line" in texts_in(folded[0])  # the producer's, folded
    assert panels.graphics(MATCH, "shots", None, "desk", None, feed_desk) == (no_update,) * 5  # hidden tab


def test_graphics_tab_without_a_feed_says_so(desk, tmp_path):
    desk.replay_dir = tmp_path
    _, _, fig, _, _ = panels.graphics(MATCH, "shots", None, "graphics", None, desk)
    assert "No feed" in fig.layout.annotations[0].text


def test_a_running_match_shows_only_what_the_desk_has_seen(feed_desk):
    path = feed_desk.root / MATCH / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(dict(state, final=False, last_seq=600)), encoding="utf-8")
    feed = feed_desk.feed(MATCH)
    assert feed["last_seq"] <= 600 and not any(a.get("voided") for a in feed["actions"])


def test_insights_panel_without_files_lists_the_feed_candidates(feed_desk):
    options, value, detail, fig, story, a_off, r_off, sa_off, sr_off, sig = panels.insights(MATCH, None, "insights", None, store=feed_desk)
    assert options and value and a_off and r_off and sa_off and sr_off
    assert "Candidate computed from the feed" in texts_in(detail) and "No storyline" in texts_in(story)
    assert len(fig.data) > 0


def test_insights_without_any_feed_or_file(desk, tmp_path):
    desk.replay_dir = tmp_path
    options, value, detail, *_ = panels.insights(MATCH, None, "insights", None, store=desk)
    assert options == [] and value is no_update and "No insight yet" in texts_in(detail)  # nothing to select: the selection is left alone


def test_insights_and_storyline_are_decided_through_the_desk_and_reach_the_overlay(feed_desk):
    docs = write_docs(feed_desk)
    run = next(d for d in docs if d["key"] == "run-demo-0007-v2-a01178")
    options, value, detail, fig, story, a_off, r_off, sa_off, sr_off, _ = panels.insights(MATCH, run["id"], "insights", None, store=feed_desk)
    assert [o["value"] for o in options] == [d["id"] for d in sorted(docs, key=lambda d: -d["value"])]
    assert not a_off and not r_off and not sa_off and "Fourth against sixth" in texts_in(story)
    assert "A quarter of a metre." in texts_in(detail) and "margin_m" not in fig.layout.meta["offside"]
    folded = find(detail, lambda c: isinstance(c, html.Details))
    assert len(folded) == 1 and "margin_m" in [c.title for c in find(folded[0], lambda c: getattr(c, "className", "") == "fact-key")]
    assert panels.decide_doc("approve", MATCH, run["id"], None, feed_desk) == ("Sign in to approve or reject.", False)
    message, done = panels.decide_doc("approve", MATCH, run["id"], "producer-1", feed_desk)
    assert done, message
    stored = {d["id"]: d for d in json.loads((feed_desk.root / MATCH / "insights.json").read_text(encoding="utf-8"))}
    assert stored[run["id"]]["status"] == "approved" and len(stored) == len(docs)
    assert len(json.loads((feed_desk.root / MATCH / "cards.json").read_text(encoding="utf-8"))) == 11  # cards untouched
    assert ("approved", "producer-1") in audit_of(feed_desk, run["id"])
    shown = overlay.render(f"?match={MATCH}&lang=en", store=feed_desk)
    assert f"Detail {run['key']}" in texts_in(shown) and "The detail" in texts_in(shown)
    assert panels.decide_doc("approve", MATCH, f"storyline-{MATCH}", "producer-1", feed_desk)[1]
    assert json.loads((feed_desk.root / MATCH / "storyline.json").read_text(encoding="utf-8"))["status"] == "approved"
    assert panels.decide_doc("approve", MATCH, "insight-unknown", "producer-1", feed_desk)[1] is False


def test_an_insight_without_texts_never_reaches_the_overlay(feed_desk):
    docs = write_docs(feed_desk, with_texts=False)
    assert panels.decide_doc("approve", MATCH, docs[0]["id"], "producer-1", feed_desk)[1]
    assert "ov-empty" in overlay.render(f"?match={MATCH}", store=feed_desk)[-1].className


def test_decisions_degrade_when_the_desk_cannot_decide_insights(feed_desk):
    docs = write_docs(feed_desk)
    feed_desk._desk = object()  # a desk without approve or reject
    message, done = panels.decide_doc("approve", MATCH, docs[0]["id"], "producer-1", feed_desk)
    assert not done and "cannot decide" in message


def test_card_detail_shows_the_duo_as_suggested_lines(desk):
    card = dict(card_at(desk, "66'"), duo=[{"speaker": "Ada", "lang": "en", "line": "He has done it again!", "claims": []},
                                           {"speaker": "Bram", "lang": "fr", "line": "Troisième but.", "claims": []}])
    text = texts_in(console.detail(card, [], lang=None))
    assert "He has done it again!" in text and "Troisième but." in text and "Suggested lines" in text and "Commentary" not in text
    english, french = texts_in(console.detail(card, [])), texts_in(console.detail(card, [], lang="fr"))
    assert "He has done it again!" in english and "Troisième but." not in english  # the language picked only
    assert "Troisième but." in french and "He has done it again!" not in french
    without = {k: v for k, v in card_at(desk, "66'").items() if k != "duo"}
    assert "Suggested lines" not in texts_in(console.detail(without, []))


# --- desk ergonomics ------------------------------------------------------------------------------

def test_decision_buttons_sit_between_the_heading_and_the_texts():
    for user in ("producer-1", None):
        page = console.layout(user)
        middle = find(page, lambda c: "col-middle" in (getattr(c, "className", "") or "") and "c-detail" in component_ids(c.children))[0]
        blocks = [component_ids(child) for child in middle.children]

        def at(cid: str) -> int:
            return next(i for i, ids in enumerate(blocks) if cid in ids)

        assert at("c-head") < at("c-approve") < at("c-edit-panel") < at("c-detail") and at("c-lang") == at("c-approve")  # one bar: decisions and language
        assert at("c-approve") == at("c-edit") == at("c-reject") and at("c-message") < at("c-detail")
        lang = find(page, lambda c: getattr(c, "id", None) == "c-lang")[0]
        assert lang.value == "en" and [o["value"] for o in lang.options] == ["en", "fr", "es"]
        actions = find(page, lambda c: getattr(c, "className", "") == "actions" and "c-approve" in component_ids(c.children))[0]
        assert getattr(actions, "style", None) == ({"display": "none"} if user is None else None)  # read only: no decision button
        assert not find(page, lambda c: "c-lang" in component_ids(getattr(c, "children", None)) and getattr(c, "style", None) == {"display": "none"})  # the switch stays


def test_the_language_switch_through_the_dash_endpoint_for_a_visitor(desk, monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    monkeypatch.setenv("EASY_AUTH_ENABLED", "true")
    goal = card_at(desk, "57'")
    text = {t["lang"]: t for t in goal["texts"]}
    shown = {}
    for lang in ("en", "fr", "es"):
        payload = dash_post(REFRESH, *refresh_body(goal["id"], lang=lang), headers=VISITOR).get_json()["response"]
        shown[lang] = json.dumps(payload["c-detail"], ensure_ascii=False)
        assert payload["c-approve"]["disabled"] is True and payload["c-edit"]["disabled"] is True and payload["c-reject"]["disabled"] is True
        assert not [i for i in json_ids(payload["c-detail"]) if isinstance(i, dict) and i.get("type", "").startswith("fb")]
        assert goal["player"]["name"] in json.dumps(payload["c-head"], ensure_ascii=False)
    for lang, t in text.items():
        assert t["why"] in shown[lang] and all(t["why"] not in shown[other] for other in shown if other != lang)
    assert console.AI_WRITTEN in shown["es"] and console.AI_WRITTEN not in shown["en"] and "machine-translated" not in shown["es"]


def test_rundown_is_in_match_order_with_the_waiting_cards_on_top(desk):
    def order() -> list[dict]:
        rundown = console.refresh(MATCH, None, None)[0]
        cards = {c["id"]: c for c in desk.cards(MATCH)}
        return [cards[o["value"]] for o in rundown]

    first = order()
    assert [c["status"] for c in first][:2] == ["pending", "pending"] and first[-1]["status"] != "pending"
    picked = first[0]
    assert console.decide("approve", MATCH, picked["id"], "producer-1")[1]
    after = order()
    waiting = [c for c in after if c["status"] == "pending"]
    assert after[:len(waiting)] == waiting and picked["id"] not in [c["id"] for c in waiting]  # decided: out of the waiting group
    rest = after[len(waiting):]
    assert [c["clock_s"] for c in rest] == sorted(c["clock_s"] for c in rest) and picked["id"] in [c["id"] for c in rest]
    assert console.refresh(MATCH, None, None)[1] == waiting[0]["id"]  # nothing selected: the top waiting card
    options = console.rundown_options(after)
    assert ["ru-waiting" in o["label"].className for o in options] == [c["status"] == "pending" for c in after]


def test_without_a_card_waiting_the_most_recent_card_is_selected(desk):
    waiting = [c for c in desk.cards(MATCH) if c["status"] == "pending"]
    assert waiting and console.refresh(MATCH, None, None)[1] == console.rundown_order(desk.cards(MATCH))[0]["id"]  # the earliest waiting card first
    for c in waiting:
        assert console.decide("reject", MATCH, c["id"], "producer-1")[1]
    cards = desk.cards(MATCH)
    assert not any(c["status"] == "pending" for c in cards)
    picked = next(c for c in cards if c["id"] == console.refresh(MATCH, None, None)[1])
    latest = max((c.get("clock_s") or 0, c.get("version") or 1) for c in cards)
    assert (picked.get("clock_s") or 0, picked.get("version") or 1) == latest  # the most recent card, not the first of the match
    earliest = min(cards, key=lambda c: c.get("clock_s") or 0)
    assert earliest["id"] != picked["id"] and console.refresh(MATCH, earliest["id"], None)[1] is no_update  # a selection is kept
    assert console.default_pick([]) is None


def test_readable_labels_for_fact_types_fields_and_audit_actions(desk):
    assert console.fact_label("ht-lead-lost") == "Half-time lead lost" and console.fact_label("ht-lead-lost-away-2") == "Half-time lead lost"
    assert console.fact_label("stat-of-the-match") == "Stat of the match" and console.fact_label("xg-ft-home-5") == "Full-time xG"
    assert console.fact_label("run-unbeaten-home") == "Unbeaten run" and console.fact_label("run-demo-0007-v2-a01178") == "Run behind the line"
    assert console.fact_label("streak") == "Streak" and console.fact_label("brand-new-thing-home-3") == "Brand new thing"
    assert console.field_label("rovers_wp_before_pct") == "Rovers win probability before (%)" and console.field_label("swing_pts") == "Swing (points)"
    assert console.field_label("shot_xg_pct") == "Shot xG (%)" and console.action_label("rewrite_requested") == "Rewrite requested"
    ruled = card_at(desk, "63'")
    history = texts_in(find(console.detail(ruled, desk.audit(MATCH)), lambda c: getattr(c, "className", "") == "history"))
    assert "Retracted" in history and "Held for a VAR check" in history and "VAR offside" in history and "var_offside" not in history and "rewrite_requested" not in history


def test_a_card_ruled_out_shows_the_var_reason_and_what_the_duo_says(desk):
    ruled = card_at(desk, "63'")
    assert ruled["status"] == "retracted" and ruled["ruled_out"]["reason"] == "offside" and ruled.get("duo_ruling")
    teams = desk.state(MATCH)["teams"]
    producer, visitor = console.detail(ruled, desk.audit(MATCH), [], teams), console.detail(ruled, desk.audit(MATCH), [], teams, read_only=True)
    assert ruled["ruled_out"]["margin_cm"] is None  # the margin and its call stay in var_decision, for the producer
    call = console.VAR_CALLS[ruled["var_decision"]["call"]]
    assert f"VAR decision: Offside, {call}" in texts_in(producer) and f"{ruled['var_decision']['margin_m']:.2f} m" in texts_in(producer)
    assert f"VAR decision: Offside, {call}" in texts_in(visitor) and " m, producer only" not in texts_in(visitor)
    for lang in ("en", "fr", "es"):
        view = texts_in(console.detail(ruled, [], [], teams, lang=lang))
        assert "On the VAR ruling" in view
        assert all(d["line"] in view for d in ruled["duo_ruling"] if d["lang"] == lang)
        assert not any(d["line"] in view for d in ruled["duo_ruling"] if d["lang"] != lang)
    ruling_keys = [i for i in feedback_ids(console.detail(ruled, [], [], teams)) if i["k"].split("|")[1] == "duo"]
    assert len(ruling_keys) == 2 * len([d for d in ruled.get("duo") or [] if d["lang"] == "en"])  # thumbs on the suggested lines only, not on the ruling
    assert "The lower-third said" not in texts_in(producer)  # it was never on air
    on_air = texts_in(console.detail(dict(ruled, retracted_on_air=True), [], [], teams))
    assert "The lower-third said: Goal ruled out (VAR, offside)" in on_air
    assert "On the VAR ruling" not in texts_in(console.detail({k: v for k, v in ruled.items() if k != "duo_ruling"}, [], [], teams))


def test_the_writing_trace_is_shown_when_the_card_carries_it(desk):
    card = card_at(desk, "57'")
    plain = console.detail(card, [])
    assert "Writer settings" not in texts_in(plain) and not find(plain, lambda c: "attempts" in (getattr(c, "className", "") or ""))
    log = [{"attempt": 1, "writer": "model-a", "verified": False, "rejected": 2, "finish_reason": "length",
            "usage": {"input_tokens": 1200, "output_tokens": 800, "reasoning_tokens": 512}, "latency_s": 4.2, "error": "unclaimed number 33"},
           {"attempt": 2, "writer": "model-a", "verified": True, "rejected": 0, "finish_reason": "stop", "usage": {"input_tokens": 1300}, "latency_s": None, "error": None},
           "not an attempt"]
    traced = dict(card, verification=dict(card["verification"], attempts_log=log, fallback_reason="every attempt refused"),
                  writer_config={"deployment": "dep-1", "effort": "low", "max_tokens": 2000, "prompt_sha": "abcdef0123456789"})
    producer, visitor = console.detail(traced, []), console.detail(traced, [], read_only=True)
    table = find(producer, lambda c: "attempts" in (getattr(c, "className", "") or ""))
    assert len(table) == 1 and len(table[0].children[1].children) == 2
    text = texts_in(producer)
    assert "1200 / 800 / 512" in text and "1300 / - / -" in text and "4.2 s" in text and "length" in text and "unclaimed number 33" in text
    assert "Written by the templates: every attempt refused" in text and "Writer settings: deployment dep-1, effort low, max_tokens 2000, prompt abcdef012345" in text
    seen = texts_in(visitor)
    assert "1200 / 800 / 512" in seen and "unclaimed number 33" not in seen and "dep-1" not in seen and "every attempt refused" not in seen  # fallback_reason: producer only
    folded = find(producer, lambda c: getattr(c, "className", "") == "producer-details")
    assert len(folded) == 1 and folded[0].open is False and texts_in(folded[0].children[0]) == "Producer details"
    inside = texts_in(folded[0])
    assert all(s in inside for s in ("unclaimed number 33", "every attempt refused", "dep-1", "abcdef012345"))
    assert "unclaimed number 33" not in texts_in(table[0])  # the error is out of the table, folded with the rest
    assert not find(visitor, lambda c: getattr(c, "className", "") == "producer-details")
    broken = dict(card, verification=dict(card["verification"], attempts_log="oops"), writer_config="oops")
    assert "Verified" in texts_in(console.detail(broken, []))  # a malformed trace is ignored


def test_overlay_never_loads_the_graphics_or_the_worker():
    code = "import sys, json, web.overlay; print(json.dumps(sorted(m for m in sys.modules if m.startswith(('worker', 'web.graphics', 'web.panels', 'web.console')))))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env={**os.environ, "PYTHONPATH": str(SRC)}).stdout
    assert json.loads(out) == []


# --- producer feedback ----------------------------------------------------------------------------

def feedback_ids(component, kind: str = "fb") -> list[dict]:
    return [i for i in component_ids_all(component) if isinstance(i, dict) and i.get("type") == kind]


def component_ids_all(component) -> list:
    """Every component id, dict ids included (they are not hashable)."""
    out = []
    if isinstance(component, (list, tuple)):
        for c in component:
            out += component_ids_all(c)
        return out
    if hasattr(component, "to_plotly_json"):
        if getattr(component, "id", None):
            out.append(component.id)
        out += component_ids_all(getattr(component, "children", None))
    return out


def test_card_detail_has_thumbs_next_to_every_sentence_and_reasons_after_a_thumbs_down(desk):
    card = card_at(desk, "57'")
    view = console.detail(card, [], lang=None)
    thumbs = feedback_ids(view)
    n_sentences = 2 * len(card["texts"]) + len(card.get("duo") or [])
    assert len(thumbs) == 2 * n_sentences and {i["v"] for i in thumbs} == {"up", "down"} and {i["p"] for i in thumbs} == {"d"}
    assert {i["k"].split("|")[2] for i in thumbs} == {"en", "fr", "es"}  # every language shown
    assert not feedback_ids(view, "fb-r") and "No producer feedback" in texts_in(view)
    for lang in ("en", "fr", "es"):  # the console shows one language at a time, its thumbs with it
        one = feedback_ids(console.detail(card, [], lang=lang))
        assert one and {i["k"].split("|")[2] for i in one} == {lang} and len(one) == 2 * (2 + len([d for d in card.get("duo") or [] if d["lang"] == lang]))
    key = f"{card['id']}|why|fr|0"
    message, done = console.give_feedback(MATCH, key, "verdict", "down", "producer-1")
    assert done and "not relevant" in message
    message, done = console.give_feedback(MATCH, key, "reason", "too_long", "producer-1")
    assert done and "too long" in message
    view = console.detail(card, [], desk.feedback_docs(MATCH), lang="fr")
    reasons = feedback_ids(view, "fb-r")
    assert {i["r"] for i in reasons} == set(console.FB.REASONS) and {i["k"] for i in reasons} == {key}
    assert feedback_ids(view, "fb-n") and "Approval so far: 0% of 1" in texts_in(view)


def test_feedback_is_stored_audited_and_keeps_the_card_as_it_was(desk):
    card = card_at(desk, "66'")
    before = json.loads((desk.root / MATCH / "cards.json").read_text(encoding="utf-8"))
    assert console.give_feedback(MATCH, f"{card['id']}|headline|en|0", "verdict", "up", "producer-1")[1]
    assert console.give_feedback(MATCH, f"{card['id']}|duo|es|0", "verdict", "down", "producer-1")[1]
    assert console.give_feedback(MATCH, f"{card['id']}|duo|es|0", "note", "Too much drama", "producer-1")[1]
    lines = [json.loads(line) for line in (desk.root / MATCH / "feedback.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3 and all(line["type"] == "feedback" for line in lines)
    latest = console.FB.by_target(lines)
    en = next(t for t in card["texts"] if t["lang"] == "en")
    assert latest[f"{card['id']}|headline|en|0"]["text"] == en["headline"] and latest[f"{card['id']}|headline|en|0"]["writer"] == "template"
    assert latest[f"{card['id']}|duo|es|0"]["note"] == "Too much drama"
    assert ("feedback", "producer-1") in audit_of(desk, card["id"])
    assert json.loads((desk.root / MATCH / "cards.json").read_text(encoding="utf-8")) == before  # a feedback is not a decision on the card
    message, done = console.decide("approve", MATCH, card["id"], "producer-1")  # never blocks approval
    assert done, message
    assert console.give_feedback(MATCH, f"{card['id']}|headline|en|0", "verdict", "up", None) == ("Sign in to give feedback.", False)
    assert not console.give_feedback(MATCH, f"{card['id']}|duo|en|99", "verdict", "up", "producer-1")[1]


def test_feedback_rebuilds_the_profile_when_the_store_keeps_one(desk, tmp_path):
    desk.profile_path = tmp_path / "feedback" / "profile.json"
    card = card_at(desk, "66'")
    console.give_feedback(MATCH, f"{card['id']}|why|en|0", "reason", "wrong_tone", "producer-1")
    profile = json.loads(desk.profile_path.read_text(encoding="utf-8"))
    assert profile["votes"] == 1 and profile["rules"][0]["source"] == "wrong_tone"


def test_insight_and_storyline_sentences_have_thumbs_candidates_do_not(feed_desk):
    out = panels.insights(MATCH, None, "insights", None, store=feed_desk)
    assert not feedback_ids(out[2]) and not feedback_ids(out[4])  # feed candidates: nothing to judge
    docs = write_docs(feed_desk)
    out = panels.insights(MATCH, docs[0]["id"], "insights", None, store=feed_desk)
    assert len(feedback_ids(out[2])) == 2 * 4 and {i["p"] for i in feedback_ids(out[2])} == {"i"}  # headline, why, two duo lines
    assert len(feedback_ids(out[4])) == 2 * 2
    message, done = console.give_feedback(MATCH, f"{docs[0]['id']}|duo|en|1", "verdict", "up", "producer-1")
    assert done, message
    again = panels.insights(MATCH, docs[0]["id"], "insights", out[-1], store=feed_desk)
    assert again[-1] != out[-1]  # the feedback redraws the panel
    stored = (feed_desk.root / MATCH / "feedback.jsonl").read_text(encoding="utf-8")
    assert '"doc_type": "insight"' in stored and '"text": "A quarter of a metre."' in stored


def test_feedback_callback_refuses_without_sign_in_in_public_mode(desk, monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    card = card_at(desk, "66'")
    key = next(k for k in app.callback_map if "c-i-rev.data@" in k)
    outputs = [{"id": part.split(".")[0], "property": part.split(".", 1)[1]} for part in key.strip(".").split("...")]
    thumb = {"k": f"{card['id']}|headline|en|0", "p": "d", "type": "fb", "v": "up"}
    body = {"output": key, "outputs": outputs,
            "inputs": [[{"id": thumb, "property": "n_clicks", "value": 1}], [], []],
            "state": [{"id": "c-match", "property": "value", "value": MATCH}, {"id": "c-i-rev", "property": "data", "value": 0}],
            "changedPropIds": [json.dumps(thumb, sort_keys=True, separators=(",", ":")) + ".n_clicks"]}
    response = app.server.test_client().post("/_dash-update-component", json=body, headers={security.PRINCIPAL_HEADER: "mallory"})
    assert response.status_code == 200 and "Sign in to give feedback" in response.get_data(as_text=True)
    assert desk.feedback_docs(MATCH) == []
    monkeypatch.setenv("PUBLIC_MODE", "0")
    response = app.server.test_client().post("/_dash-update-component", json=body)
    assert "relevant, by Producer" in response.get_data(as_text=True)
    assert len(desk.feedback_docs(MATCH)) == 1
