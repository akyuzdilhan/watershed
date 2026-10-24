import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dash import no_update
from flask import Flask

from generator import generate_match
from web import console, overlay, security, store
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

def test_public_mode_requires_a_signed_in_producer(desk, monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    monkeypatch.setenv("EASY_AUTH_ENABLED", "true")
    with app.server.test_request_context("/"):
        assert security.producer() is None
        assert "Sign in" in texts_in(route("/"))
        assert "Sign in" not in texts_in(route("/overlay"))  # the overlay stays public
    with app.server.test_request_context("/", headers={security.PRINCIPAL_HEADER: "ana@example.com"}):
        assert security.producer() == "ana@example.com"
        assert "Signed in as ana@example.com" in texts_in(route("/"))
    monkeypatch.setenv("PRODUCERS", "bob@example.com")
    with app.server.test_request_context("/", headers={security.PRINCIPAL_HEADER: "ana@example.com"}):
        assert security.producer() is None
    monkeypatch.delenv("PRODUCERS")
    monkeypatch.setenv("EASY_AUTH_ENABLED", "false")  # without Easy Auth the header is forgeable
    with app.server.test_request_context("/", headers={security.PRINCIPAL_HEADER: "ana@example.com"}):
        assert security.producer() is None


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
