import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dash import no_update
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
    assert done and "approved by producer-1" in message
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
    message, done = console.decide("approve-edited", MATCH, card["id"], "producer-1", {"en": {"headline": "Lightfoot again!", "why": "Rovers jump from 8% to 32% to win."}})
    assert done, message
    after = card_at(desk, "57'")
    en = next(t for t in after["texts"] if t["lang"] == "en")
    assert after["status"] == "approved" and en["headline"] == "Lightfoot again!"
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
    rundown, selected, detail, fig, score, approve_off, edit_off, reject_off, sig = out
    assert len(rundown) == 11 and selected == card_at(desk, "66'")["id"]  # the biggest pending swing
    assert "Retracted" in texts_in([o["label"] for o in rundown])
    assert "machine-translated" in texts_in(detail)
    assert [t.name for t in fig.data[:3]] == ["Rovers win", "Draw", "United win"]
    assert {t.name for t in fig.data[3:]} == {"Cards", "Retracted cards"}
    assert len(fig.layout.shapes) == 1  # half time
    assert "5-2" in score and not approve_off and not edit_off and not reject_off
    again = console.refresh(MATCH, selected, sig)
    assert all(v is no_update for v in again)  # nothing changed: nothing is sent


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
    view = console.detail(save, desk.audit(MATCH), [], teams)
    text = texts_in(view)
    for c in worth:
        assert f"{c['value']:g} {c['source_field']} {meanings[c['source_field']]}" in text  # the number, its field, what the field means
    blocks = text_blocks(view)
    assert len(blocks) == len(save["texts"])
    for block, t in zip(blocks, save["texts"]):
        shown = texts_in(block)
        assert "Verifier: passes" in shown and "every number claimed" in shown and "claims match the facts" in shown
        assert all(f"{c['value']:g} {c['source_field']}" in shown for c in t["claims"])
    assert "Facts" not in text  # the raw facts table is gone
    reps = save["verification"].get("repetition_warnings", 0)
    assert (f"{reps} repetition warning(s)" if reps else "no repetition warning") in text
    edited = dict(save, texts=[dict(t, why=t["why"] + " 33%.") if t["lang"] == "en" else t for t in save["texts"]])  # a number no fact backs
    blocks = text_blocks(console.detail(edited, [], [], teams))
    assert "Verifier: fails" in texts_in(blocks[0]) and "every number claimed: 33" in texts_in(blocks[0])
    assert all("Verifier: passes" in texts_in(b) for b in blocks[1:])


def test_a_scorer_correction_is_shown_in_the_console_and_on_the_overlay(desk):
    rewritten = next(c for c in desk.cards(MATCH) if c.get("version", 1) >= 2)
    first = next(m for m in matchfeed.load(REPLAY_DIR / f"{MATCH}.jsonl.gz") if m["type"] == "action" and m["event_id"] == rewritten["event_id"])  # the goal as first credited
    previous, now = first["player"]["name"], rewritten["player"]["name"]
    assert previous != now
    corrected = dict(rewritten, previous_player=previous)
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
    assert "VAR decision" not in texts_in(console.detail(card_at(desk, "63'"), []))
    cards = json.loads((desk.root / MATCH / "cards.json").read_text(encoding="utf-8"))
    (desk.root / MATCH / "cards.json").write_text(json.dumps([dict(c, var_decision=decision) if c["id"] == ruled["id"] else c for c in cards]), encoding="utf-8")
    monkeypatch.setenv("PUBLIC_MODE", "1")
    with app.server.test_request_context("/"):
        shown = texts_in(console._refresh_cb(1, 0, MATCH, ruled["id"], None)[2])
    assert "tight call" in shown and "0.37" not in shown
    monkeypatch.setenv("PUBLIC_MODE", "0")
    with app.server.test_request_context("/"):
        assert "0.37 m" in texts_in(console._refresh_cb(1, 0, MATCH, ruled["id"], None)[2])
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
    path.write_text(json.dumps(dict(state, last_seq=state["last_seq"] + 1, minute_label="90+12'")), encoding="utf-8")
    assert console.decide("approve", MATCH, card_at(desk, "14'")["id"], "producer-1")[1]
    second = dash_post(REFRESH, *refresh_body(goal["id"], first["c-sig"]["data"], 2)).get_json()["response"]
    assert "c-detail" not in second and "90+12'" in second["c-score"]["children"] and "c-rundown" in second  # the note field is not drawn again: the draft stays
    assert console.give_feedback(MATCH, key, "reason", "too_long", "producer-1")[1]  # a change on this card draws it again
    third = dash_post(REFRESH, *refresh_body(goal["id"], second["c-sig"]["data"], 3)).get_json()["response"]
    assert "c-detail" in third
    assert console.refresh(MATCH, goal["id"], third["c-sig"]["data"], rev=0) == (no_update,) * 9  # nothing changed since: nothing sent


# --- overlay --------------------------------------------------------------------------------------

def test_overlay_shows_the_latest_approved_card_in_the_chosen_language(desk):
    assert "ov-empty" in overlay.render("?lang=fr")[-1].className  # nothing approved yet
    console.decide("approve", MATCH, card_at(desk, "66'")["id"], "producer-1")
    console.decide("approve", MATCH, card_at(desk, "57'")["id"], "producer-1")
    shown = overlay.render(f"?match={MATCH}&lang=fr")
    fr_headline = next(t["headline"] for t in card_at(desk, "57'")["texts"] if t["lang"] == "fr")  # the templates vary their wording within a match
    assert fr_headline.startswith("Dorian Lightfoot") and fr_headline in texts_in(shown) and "57'" in texts_in(shown)
    es = texts_in(overlay.render("?lang=es&clean=1"))
    assert "traducción automática" in es and "EN" not in es
    console.decide("reject", MATCH, card_at(desk, "57'")["id"], "producer-1")  # taken off air: the previous card is back
    assert "66'" in texts_in(overlay.render("?lang=en"))


def test_overlay_shows_the_retraction_of_a_card_that_was_on_air(tmp_path, monkeypatch):
    run_desk(tmp_path, auto_approve=True, stop_at_first_retract=True)
    s = store.FileStore(tmp_path)
    s.cache_s = 0.0
    item = overlay.on_air(s.cards(MATCH), s.audit(MATCH))
    assert item["kind"] == "retraction" and item["card"]["minute_label"] == "63'"
    text = texts_in(overlay.render("?lang=en", store=s))
    assert "Goal ruled out: card withdrawn" in text and "63'" in text
    assert "But refusé" in texts_in(overlay.render("?lang=fr", store=s))


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
    assert shown(3060.0 + overlay.CARD_HOLD_S + 1, docs=(retracted, fact), audit=taken_back)["card"]["id"] == fact["id"]


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
        if minute == "57'":  # the card's time is up: the latest fact takes the screen
            until(lambda rs: s.state(MATCH)["clock_s"] >= goal["clock_s"] + overlay.CARD_HOLD_S)
            item, text = shown()
            assert item["card"]["id"] == facts[-1] and headline not in text
        if minute == "63'":  # ruled out after VAR: the retraction is on screen, not a fact
            until(lambda rs: any(r.get("action") == "retract" for r in rs))
            item, text = shown()
            assert item["kind"] == "retraction" and item["card"]["id"] == goal["id"] and "Goal ruled out: card withdrawn" in text


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
        assert "Signed in as ana@example.com" in texts_in(page) and "Read-only view" not in texts_in(page) and "read-only" not in page.className
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
    assert "approved by local-producer" in response.get_data(as_text=True)
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


def refresh_body(card_id: str, sig=None, n: int = 1) -> tuple[list, list, list]:
    inputs = [{"id": "c-tick", "property": "n_intervals", "value": n}, {"id": "c-rev", "property": "data", "value": 0},
              {"id": "c-match", "property": "value", "value": MATCH}, {"id": "c-rundown", "property": "value", "value": card_id}]
    return inputs, [{"id": "c-sig", "property": "data", "value": sig}], ["c-tick.n_intervals"]


def test_visitors_read_the_desk_without_producer_controls_or_data(desk, monkeypatch):
    goal = card_at(desk, "66'")
    assert console.decide("approve", MATCH, goal["id"], "ana@example.com")[1]
    assert console.give_feedback(MATCH, f"{goal['id']}|headline|en|0", "verdict", "down", "ana@example.com")[1]
    monkeypatch.setenv("PUBLIC_MODE", "1")
    monkeypatch.setenv("EASY_AUTH_ENABLED", "true")
    with app.server.test_request_context("/"):
        rundown, _, detail, fig, score, approve_off, edit_off, reject_off, _ = console._refresh_cb(1, 0, MATCH, goal["id"], None)
    text = texts_in(detail)
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
        detail = console._refresh_cb(1, 0, MATCH, goal["id"], None)[2]
    assert feedback_ids(detail) and feedback_ids(detail, "fb-n") and "ana@example.com" in texts_in(detail)


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
    assert "approved by ana@example.com" in dash_post(DECIDE, *decision("c-approve", goal), headers=producer).get_data(as_text=True)
    assert "Rejected by ana@example.com" in dash_post(DECIDE, *decision("c-reject", other), headers=producer).get_data(as_text=True)
    assert "not relevant (too long), by ana@example.com" in dash_post("c-i-rev.data@", *judge("fb-r", goal, "too_long"), headers=producer).get_data(as_text=True)
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
            "    out = console._refresh_cb(1, 0, sys.argv[1], None, None)\n"
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
    _, _, fig, _, _ = panels.graphics(MATCH, "tactics", "run:demo-0007-v2-a01178:home-20", "graphics", None, feed_desk)
    assert fig.layout.meta["offside"]["margin_m"] == 0.25
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
    assert "A quarter of a metre." in texts_in(detail) and fig.layout.meta["offside"]["margin_m"] == 0.25
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
    text = texts_in(console.detail(card, []))
    assert "He has done it again!" in text and "Troisième but." in text and "Suggested lines" in text and "Commentary" not in text
    without = {k: v for k, v in card_at(desk, "66'").items() if k != "duo"}
    assert "Suggested lines" not in texts_in(console.detail(without, []))


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
    view = console.detail(card, [])
    thumbs = feedback_ids(view)
    n_sentences = 2 * len(card["texts"]) + len(card.get("duo") or [])
    assert len(thumbs) == 2 * n_sentences and {i["v"] for i in thumbs} == {"up", "down"} and {i["p"] for i in thumbs} == {"d"}
    assert {i["k"].split("|")[2] for i in thumbs} == {"en", "fr", "es"}  # every language shown
    assert not feedback_ids(view, "fb-r") and "No producer feedback" in texts_in(view)
    key = f"{card['id']}|why|fr|0"
    message, done = console.give_feedback(MATCH, key, "verdict", "down", "producer-1")
    assert done and "not relevant" in message
    message, done = console.give_feedback(MATCH, key, "reason", "too_long", "producer-1")
    assert done and "too long" in message
    view = console.detail(card, [], desk.feedback_docs(MATCH))
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
    assert "relevant, by local-producer" in response.get_data(as_text=True)
    assert len(desk.feedback_docs(MATCH)) == 1
