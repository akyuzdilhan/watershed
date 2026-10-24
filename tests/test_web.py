import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dash import no_update
from flask import Flask

from generator import generate_match
from web import console, overlay, panels, security, store
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


def test_card_detail_shows_the_commentary_duo(desk):
    card = dict(card_at(desk, "66'"), duo=[{"speaker": "Ada", "lang": "en", "line": "He has done it again!", "claims": []},
                                           {"speaker": "Bram", "lang": "fr", "line": "Troisième but.", "claims": []}])
    text = texts_in(console.detail(card, []))
    assert "He has done it again!" in text and "Troisième but." in text and "Commentary" in text
    without = {k: v for k, v in card_at(desk, "66'").items() if k != "duo"}
    assert "Commentary" not in texts_in(console.detail(without, []))


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
