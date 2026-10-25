import json
import math
import statistics
import sys
from pathlib import Path

import pytest
from dash import no_update

from generator import generate_match
from web import graphics, matchfeed, panels, security, store
from web.app import app
from worker import recap, tactics
from worker.desk import Desk
from worker.pipeline import JsonFileSink, Pipeline
from worker.writer import TemplateWriter

MATCH = "demo-0007"
REPLAY = Path(__file__).resolve().parents[1] / "data" / "replay" / f"{MATCH}.jsonl.gz"
ON_INSIGHTS = {"c-match.value": MATCH, "c-view.value": "insights", "c-i-rev.data": 0}  # callback values, by "id.property"
ON_GRAPHICS = {"c-match.value": MATCH, "c-view.value": "graphics"}
PRODUCER = {security.PRINCIPAL_HEADER: "producer@example.com"}  # with EASY_AUTH_ENABLED: a signed-in producer; without the header, a visitor


def texts_in(component) -> str:
    """Every string in a Dash component tree."""
    if component is None:
        return ""
    if isinstance(component, (str, int, float)):
        return str(component)
    if isinstance(component, (list, tuple)):
        return " ".join(texts_in(c) for c in component)
    return texts_in(getattr(component, "children", None))


@pytest.fixture(scope="module")
def approved_desk(tmp_path_factory):
    """The demo match through the pipeline with every card approved, as on the judges' run."""
    root = tmp_path_factory.mktemp("desk")
    stream, _ = generate_match(7, "demo")
    sink = JsonFileSink(root)
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True))
    for m in stream:
        pipeline.handle(m)
    pipeline.flush()
    return root


@pytest.fixture
def desk(approved_desk, tmp_path, monkeypatch):
    """A fresh copy of that desk, served as the process store, outside public mode."""
    root = tmp_path / "desk"
    (root / MATCH).mkdir(parents=True)
    for name in ("cards.json", "state.json", "audit.jsonl"):
        (root / MATCH / name).write_bytes((approved_desk / MATCH / name).read_bytes())
    s = store.FileStore(root, replay_dir=tmp_path)
    s.cache_s = 0.0
    store.set_store(s)
    monkeypatch.setenv("PUBLIC_MODE", "0")
    yield s
    store.set_store(None)


def write_recap(s: store.FileStore, monkeypatch) -> dict:
    """recap.json as `python -m worker.recap <desk>/<match>` writes it (template writer)."""
    monkeypatch.setattr(sys, "argv", ["worker.recap", str(s.root / MATCH)])
    recap.main()
    return json.loads((s.root / MATCH / "recap.json").read_text(encoding="utf-8"))


def set_final(s: store.FileStore, final: bool) -> None:
    path = s.root / MATCH / "state.json"
    path.write_text(json.dumps(dict(json.loads(path.read_text(encoding="utf-8")), final=final)), encoding="utf-8")


def text_blocks(section) -> list:
    return [c for c in section.children if getattr(c, "className", None) == "text-block"]


def block_lang(block) -> tuple[str, bool]:
    """(language shown, labelled machine-translated) of one text block."""
    row = block.children[0].children
    return row[0].children, any(getattr(c, "className", "") == "badge st-mt" for c in row[1:])


def dash_post(output: str, values: dict, changed: str, headers: dict | None = None) -> dict:
    """POSTs to the callback with `output` ("id.property") among its outputs, as the browser does, and returns the outputs it set. `values` by "id.property", None for the others."""
    key = next(k for k in app.callback_map if output in k.strip(".").split("..."))
    spec = app.callback_map[key]
    arg = lambda d: {"id": d["id"], "property": d["property"], "value": values.get(f"{d['id']}.{d['property']}")}  # noqa: E731
    body = {"output": key, "outputs": [{"id": p.split(".")[0], "property": p.split(".", 1)[1]} for p in key.strip(".").split("...")],
            "inputs": [arg(d) for d in spec["inputs"]], "state": [arg(d) for d in spec["state"]], "changedPropIds": [changed]}
    response = app.server.test_client().post("/_dash-update-component", json=body, headers=headers or {})
    assert response.status_code in (200, 204), response.get_data(as_text=True)[:400]
    return response.get_json()["response"] if response.status_code == 200 else {}


def components(node):
    """Every component of a callback answer (Dash JSON), radio labels included."""
    if isinstance(node, list):
        for n in node:
            yield from components(n)
    elif isinstance(node, dict):
        if "props" in node and "type" in node:
            yield node
            yield from components(node["props"].get("children"))
        else:
            for v in node.values():
                yield from components(v)


def words(node) -> str:
    """Every string shown by a callback answer."""
    out = []
    for c in components(node):
        kids = c["props"].get("children")
        out += [str(k) for k in (kids if isinstance(kids, list) else [kids]) if isinstance(k, (str, int, float)) and not isinstance(k, bool)]
    return " ".join(out)


def feedback_ids(node) -> list:
    return [c["props"]["id"] for c in components(node) if isinstance(c["props"].get("id"), dict) and c["props"]["id"].get("type") in ("fb", "fb-r", "fb-n")]


def fact_keys(node) -> set:
    return {c["props"]["children"] for c in components(node) if c["props"].get("className") == "fact-key"}


# --- after the match --------------------------------------------------------------------------------

def test_the_store_reads_the_recap_the_cli_writes(desk, monkeypatch):
    assert desk.recap(MATCH) is None
    doc = write_recap(desk, monkeypatch)
    assert desk.recap(MATCH) == doc and doc["id"] == f"recap-{MATCH}" and doc["type"] == "recap"
    (desk.root / MATCH / "recap.json").write_text("{\"id\": ", encoding="utf-8")  # the worker rewriting it
    assert desk.recap(MATCH) == doc


def test_after_the_match_shows_the_recap_the_supporters_and_the_analyst_text_in_every_language(desk, monkeypatch):
    doc = write_recap(desk, monkeypatch)
    assert doc["final"] and doc["verification"]["verified"]
    section, sig = panels.after_the_match(MATCH, "insights", None, desk)
    text = texts_in(section)
    for t in doc["texts"]:
        assert t["recap"] in text and t["fan"] in text and t["analyst"] in text, t["lang"]
    assert f"Supporters ({doc['fan_team']}): " in text and f"Analyst ({doc['analyst_team']}): " in text and "Verified" in text
    shown = [block_lang(b) for b in text_blocks(section)]
    assert shown == [(t["lang"].upper(), t["lang"] == "es") for t in doc["texts"]] and [t["lang"] for t in doc["texts"]] == ["en", "fr", "es"]
    assert doc["machine_translated"] == ["es"]
    assert panels.after_the_match(MATCH, "insights", sig, desk) == (no_update, no_update)  # unchanged: nothing re-sent
    assert panels.after_the_match(MATCH, "desk", None, desk) == (no_update, no_update)  # hidden tab
    french = panels.recap_block(doc, True, "fr")
    assert [block_lang(b)[0] for b in text_blocks(french)] == ["FR"]


def test_before_the_final_whistle_the_section_says_when_the_recap_comes(desk, monkeypatch):
    set_final(desk, False)
    assert texts_in(panels.after_the_match(MATCH, "insights", None, desk)[0]) == "The recap appears at full time."
    write_recap(desk, monkeypatch)  # a recap left by an earlier run, or written before the whistle
    assert texts_in(panels.after_the_match(MATCH, "insights", None, desk)[0]) == "The recap appears at full time."
    assert texts_in(panels.after_the_match(None, "insights", None, desk)[0]) == "The recap appears at full time."
    set_final(desk, True)
    (desk.root / MATCH / "recap.json").unlink()
    assert "no recap yet" in texts_in(panels.after_the_match(MATCH, "insights", None, desk)[0])


def test_the_section_sits_in_the_insights_pane_and_comes_with_its_refresh(desk, monkeypatch):
    pane = panels.insights_pane()
    assert "After the match" in texts_in(pane) and "The recap appears at full time." in texts_in(pane)
    doc = write_recap(desk, monkeypatch)
    key = next(k for k in app.callback_map if "c-r-detail.children" in k)
    assert "c-i-detail.children" in key  # the insights refresh carries it: no extra request per tick
    answer = dash_post("c-r-detail.children", {**ON_INSIGHTS, "c-i-tick.n_intervals": 1}, "c-i-tick.n_intervals")
    en = next(t for t in doc["texts"] if t["lang"] == "en")
    assert en["recap"] in json.dumps(answer["c-r-detail"]["children"], ensure_ascii=False) and answer["c-r-sig"]["data"]


class FakeCards:
    """The `cards` container, read by id."""

    def __init__(self, docs: list[dict]) -> None:
        self.docs = {(d["match_id"], d["id"]): d for d in docs}

    def read_item(self, item: str, partition_key: str) -> dict:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            return dict(self.docs[(partition_key, item)])
        except KeyError:
            raise CosmosResourceNotFoundError(status_code=404, message=f"{item} not found") from None


def test_the_cosmos_store_reads_the_recap_by_its_id():
    pytest.importorskip("azure.cosmos")
    s = store.CosmosStore.__new__(store.CosmosStore)  # no client: the container is faked
    store.Store.__init__(s)
    doc = {"id": f"recap-{MATCH}", "type": "recap", "match_id": MATCH, "final": True, "texts": [{"lang": "en", "recap": "Full time.", "fan": "", "analyst": ""}]}
    s.cards_c = FakeCards([dict(doc, _rid="x", _ts=1), {"id": f"state-{MATCH}", "type": "match_state", "match_id": MATCH, "final": True}])
    assert s.recap(MATCH) == doc and s.recap("demo-0008") is None
    assert s.state(MATCH)["final"] is True and s.storyline(MATCH) is None
    assert "Full time." in texts_in(panels.after_the_match(MATCH, "insights", None, s)[0])


# --- match graphics ---------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def demo_feed():
    return matchfeed.build(matchfeed.load(REPLAY))


def test_the_shot_map_totals_leave_out_the_shots_var_ruled_out(demo_feed):
    shots = graphics.shots(demo_feed)
    ruled_out = [a for a in shots if a.get("voided")]
    assert {a["team"] for a in ruled_out} == {"home", "away"}  # one goal ruled out each on the demo
    fig = graphics.shot_map(demo_feed)
    teams = matchfeed.team_names(demo_feed["info"])
    header = {a.text.split("</b>")[0].removeprefix("<b>"): a.text for a in fig.layout.annotations}
    for side in ("home", "away"):
        counted = [a for a in shots if a["team"] == side and not a.get("voided")]
        goals = sum(tactics.counted_goal(a) for a in counted)
        assert header[teams[side]].endswith(f" {goals} goals, {len(counted)} shots, xG {sum(a['shot']['xg'] for a in counted):.2f}"), side
    label, color, symbol, _ = graphics.OUTCOMES["voided"]
    crosses = next(t for t in fig.data if t.name == label)
    assert sorted(crosses.ids) == sorted(a["event_id"] for a in ruled_out) and crosses.marker.symbol == symbol and crosses.marker.color == color  # still drawn
    series = graphics.momentum_series(demo_feed)
    for side in ("home", "away"):
        assert math.isclose(sum(p["xg"] for p in series[side]), sum(a["shot"]["xg"] for a in shots if a["team"] == side and not a.get("voided")), abs_tol=0.01)


def test_the_former_pressing_figure_is_named_for_what_it_counts(demo_feed):
    fig = graphics.pressing_figure(demo_feed)
    assert fig.layout.title.text.startswith("<b>Opponents near the ball</b>") and f"within {tactics.PRESS_RADIUS_M:g} m of the opponent on the ball" in fig.layout.title.text
    assert "Pressing" not in fig.layout.title.text
    labels = {o["value"]: o["label"] for o in panels.graphic_items(demo_feed, "tactics")}
    assert labels["press"] == "Opponents near the ball over time"
    assert panels.graphic_figure(demo_feed, "tactics", "press").layout.title.text == fig.layout.title.text


def test_the_line_height_figure_plots_means_and_says_so(demo_feed):
    fig = graphics.line_height_figure(demo_feed)
    assert "mean distance" in fig.layout.title.text and "median" not in fig.layout.title.text
    by_slice: dict = {}
    for r in tactics.line_heights(demo_feed["actions"]):
        if r["in_possession"] != "home":
            by_slice.setdefault(tactics.bucket(r), []).append(r["home"])
    home = next(t for t in fig.data if t.name == matchfeed.team_names(demo_feed["info"])["home"])
    assert list(home.y) == [round(statistics.fmean(v), 2) for _, v in sorted(by_slice.items())]


# --- read-only view (public mode) -------------------------------------------------------------------

@pytest.fixture
def public(desk, monkeypatch):
    """The demo desk with its feed, in public mode with Easy Auth: a request without the sign-in header is a visitor's."""
    desk.replay_dir = REPLAY.parent
    monkeypatch.setenv("PUBLIC_MODE", "1")
    monkeypatch.setenv("EASY_AUTH_ENABLED", "true")
    return desk


def write_docs(s: store.FileStore) -> list[dict]:
    """The feed's tactical candidates and a league-history fact as insights on the desk, with texts and duo lines, and a storyline."""
    feed = s.feed(MATCH)
    teams = s.state(MATCH)["teams"]
    rare = {"id": f"insight-{MATCH}-rare", "type": "insight", "match_id": MATCH, "key": "rare", "kind": "record", "value": 95, "minute_label": "90'",
            "headline_facts": {"team": teams["home"], "one_in": 101, "surprisal_bits": 6.7, "history_count": 94, "history_opportunities": 9579, "history_per_season": 1.9,
                               "history_seasons": 50, "last_season": "2019/20"},
            "field_meanings": {"team": "the team", "one_in": "about once in this many matches", "surprisal_bits": "rarity in bits", "history_count": "times in the history",
                               "history_opportunities": "matches in the history", "history_per_season": "times a season", "history_seasons": "seasons of history",
                               "last_season": "last time"},
            "rarity": {"p": 0.0099, "surprisal_bits": 6.66, "basis": "wins from two down: 94 in 9,579 matches since 1976/77"}}
    docs = tactics.insight_candidates(feed["actions"], feed["info"], MATCH) + [rare]
    for d in docs:
        d["status"] = "pending"
        d["texts"] = [{"lang": "en", "headline": f"Detail {d['key']}", "why": "Checked against the frames.", "claims": []}]
        d["duo"] = [{"speaker": "wren", "lang": "en", "line": "Look at that!", "claims": []}, {"speaker": "joss", "lang": "en", "line": "Measured on the frame.", "claims": []}]
    (s.root / MATCH / "insights.json").write_text(json.dumps(docs), encoding="utf-8")
    (s.root / MATCH / "storyline.json").write_text(json.dumps({"id": f"storyline-{MATCH}", "type": "storyline", "match_id": MATCH, "status": "pending",
                                                               "facts": {"home_rank": 4}, "field_meanings": {"home_rank": "home table position"},
                                                               "texts": [{"lang": "en", "headline": "Fourth against sixth", "why": "A top-four duel."}]}), encoding="utf-8")
    return docs


def official_run(feed: dict) -> tuple[str, float]:
    """(graphics item, official margin) of the run VAR measured on the demo."""
    run = next(r for r in tactics.runs_behind(feed["actions"]) if r["official"])
    return f"run:{run['event_id']}:{run['runner_id']}", run["margin_m"]


def margins_in(fig: dict) -> list[str]:
    """What a run figure says about the margin: the annotation named margin, the meta's margin fields, a title with metres before beyond or behind."""
    layout = fig["layout"]
    meta = (layout.get("meta") or {}).get("offside") or {}
    title = layout["title"]["text"]
    return ([a.get("text", "") for a in layout.get("annotations") or [] if a.get("name") == "margin"] + [k for k in ("margin_m", "frame_margin_m", "annotated_m") if k in meta]
            + ([title] if " m beyond" in title or " m behind" in title else []))


def test_a_visitor_sees_the_graphics_without_the_offside_margins(public):
    feed = public.feed(MATCH)
    item, margin = official_run(feed)
    values = {**ON_GRAPHICS, "c-g-kind.value": "tactics", "c-g-item.value": item}
    seen = {who: dash_post("c-g-figure.figure", values, "c-g-item.value", headers) for who, headers in (("visitor", {}), ("producer", PRODUCER))}
    labels = {who: {o["value"]: o["label"] for o in out["c-g-item"]["options"]} for who, out in seen.items()}
    runs = [v for v in labels["producer"] if v.startswith("run:")]
    assert runs and labels["visitor"].keys() == labels["producer"].keys()  # the same items, every run listed
    assert f"{abs(margin):.2f} m beyond the line" in labels["producer"][item]
    for v in runs:
        assert " m beyond" not in labels["visitor"][v] and " m inside" not in labels["visitor"][v] and labels["visitor"][v].endswith(" the line")
    assert margins_in(seen["producer"]["c-g-figure"]["figure"]) and margins_in(seen["visitor"]["c-g-figure"]["figure"]) == []
    visitor = seen["visitor"]["c-g-figure"]["figure"]
    assert visitor["layout"]["meta"]["offside"]["line_x"] and "beyond the second-last defender" in visitor["layout"]["title"]["text"]  # the line and the call stay
    shots = dash_post("c-g-figure.figure", {**ON_GRAPHICS, "c-g-kind.value": "shots"}, "c-g-kind.value")
    assert shots["c-g-figure"]["figure"]["data"]  # every other graphic as the producer sees it


def test_a_visitor_sees_insights_storyline_and_recap_read_only(public, monkeypatch):
    feed = public.feed(MATCH)
    candidates = tactics.insight_candidates(feed["actions"], feed["info"], MATCH)
    run_margins = [f"{d['headline_facts']['margin_m']} m" for d in candidates if d["figure"]["kind"] == "run"]
    listed = dash_post("c-r-detail.children", {**ON_INSIGHTS, "c-i-tick.n_intervals": 1}, "c-i-tick.n_intervals")  # no insight on the desk: the feed's candidates
    assert sum(len(props) for props in listed.values()) == 12  # every output of the callback, sent to a visitor
    assert run_margins and not any(m in words(listed["c-i-list"]["options"]) for m in run_margins)
    doc = write_recap(public, monkeypatch)
    docs = write_docs(public)
    run = next(d for d in docs if d["figure"].get("kind") == "run" and d["headline_facts"].get("margin_m"))
    rare = docs[-1]
    seen = {}
    for who, headers in (("visitor", {}), ("producer", PRODUCER)):
        for d in (run, rare):
            seen[who, d["key"]] = dash_post("c-r-detail.children", {**ON_INSIGHTS, "c-i-list.value": d["id"]}, "c-i-list.value", headers)
    en = next(t for t in doc["texts"] if t["lang"] == "en")
    for key in (run["key"], rare["key"]):
        visitor, producer = seen["visitor", key], seen["producer", key]
        assert [visitor[b]["disabled"] for b in ("c-i-approve", "c-i-reject", "c-s-approve", "c-s-reject")] == [True] * 4
        assert [producer[b]["disabled"] for b in ("c-i-approve", "c-i-reject", "c-s-approve", "c-s-reject")] == [False] * 4
        assert not feedback_ids([visitor["c-i-detail"], visitor["c-s-detail"]]) and feedback_ids(producer["c-i-detail"]) and feedback_ids(producer["c-s-detail"])
        assert "Fourth against sixth" in words(visitor["c-s-detail"]) and "Measured on the frame." in words(visitor["c-i-detail"])
        assert "Rarity:" in words(producer["c-i-detail"]) and "Rarity:" not in words(visitor["c-i-detail"])
        assert en["recap"] in words(visitor["c-r-detail"])  # After the match, for every visitor
    hidden = set(rare["headline_facts"]) & panels.PRODUCER_ONLY_FACTS
    assert hidden and fact_keys(seen["producer", rare["key"]]["c-i-detail"]) == set(rare["headline_facts"])
    assert fact_keys(seen["visitor", rare["key"]]["c-i-detail"]) == set(rare["headline_facts"]) - hidden
    assert panels.PRODUCER_ONLY_NOTE in words(seen["visitor", rare["key"]]["c-i-detail"]) and panels.PRODUCER_ONLY_NOTE not in words(seen["producer", rare["key"]]["c-i-detail"])
    assert "margin_m" in fact_keys(seen["producer", run["key"]]["c-i-detail"]) and "margin_m" not in fact_keys(seen["visitor", run["key"]]["c-i-detail"])
    assert margins_in(seen["producer", run["key"]]["c-i-figure"]["figure"]) and margins_in(seen["visitor", run["key"]]["c-i-figure"]["figure"]) == []


def test_a_visitor_cannot_approve_or_reject_an_insight_or_the_storyline(public):
    docs = write_docs(public)
    folder = public.root / MATCH
    before = {name: (folder / name).read_bytes() for name in ("insights.json", "storyline.json", "audit.jsonl")}
    values = {"c-match.value": MATCH, "c-i-list.value": docs[0]["id"], "c-i-rev.data": 0}
    for button in ("c-i-approve", "c-i-reject", "c-s-approve", "c-s-reject"):
        out = dash_post("c-i-message.children", {**values, f"{button}.n_clicks": 1}, f"{button}.n_clicks")
        assert "Sign in to approve or reject" in words(out["c-i-message"]) and "c-i-rev" not in out
    assert {name: (folder / name).read_bytes() for name in before} == before  # nothing written
    out = dash_post("c-i-message.children", {**values, "c-i-approve.n_clicks": 1}, "c-i-approve.n_clicks", PRODUCER)
    assert f"On air, by {PRODUCER[security.PRINCIPAL_HEADER]}" in words(out["c-i-message"]) and out["c-i-rev"]["data"] == 1
    assert {d["id"]: d for d in public.insights(MATCH)}[docs[0]["id"]]["status"] == "approved"
