import json
import math
import os
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

import pytest
from dash import no_update

from generator import generate_match
from web import graphics, labels, matchfeed, overlay, panels, security, store
from web.app import app
from worker import recap, tactics
from worker.desk import Desk
from worker.duo import PERSONAS
from worker.pipeline import JsonFileSink, Pipeline
from worker.writer import TemplateWriter

MATCH = "demo-0007"
REPLAY = Path(__file__).resolve().parents[1] / "data" / "replay" / f"{MATCH}.jsonl.gz"
ON_INSIGHTS = {"c-p-match.data": MATCH, "c-view.value": "insights", "c-i-rev.data": 0}  # callback values, by "id.property"
ON_GRAPHICS = {"c-p-match.data": MATCH, "c-view.value": "graphics"}
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


def find(component, test) -> list:
    """Every component of a Dash tree for which `test` is true."""
    if isinstance(component, (list, tuple)):
        return [c for item in component for c in find(item, test)]
    if not hasattr(component, "to_plotly_json"):
        return []
    return ([component] if test(component) else []) + find(getattr(component, "children", None), test)


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
    """(language shown, labelled "AI-written, not reviewed") of one text block."""
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


def unfolded(node):
    """The components of a callback answer outside any "Producer details" (an `html.Details`), what a screen capture shows with the details closed."""
    if isinstance(node, list):
        for n in node:
            yield from unfolded(n)
    elif isinstance(node, dict):
        if "props" in node and "type" in node:
            if node["type"] == "Details":
                return
            yield node
            yield from unfolded(node["props"].get("children"))
        else:
            for v in node.values():
                yield from unfolded(v)


def unfolded_words(node) -> str:
    """Every string shown with the producer details closed."""
    out = []
    for c in unfolded(node):
        kids = c["props"].get("children")
        out += [str(k) for k in (kids if isinstance(kids, list) else [kids]) if isinstance(k, (str, int, float)) and not isinstance(k, bool)]
    return " ".join(out)


def folded_words(node) -> str:
    """Every string under "Producer details"."""
    return " ".join(words(c) for c in components(node) if c["type"] == "Details")


def feedback_ids(node) -> list:
    return [c["props"]["id"] for c in components(node) if isinstance(c["props"].get("id"), dict) and c["props"]["id"].get("type") in ("fb", "fb-r", "fb-n")]


def fact_keys(node) -> set:
    """The fact fields of a callback answer: each row shows the field in words, with its key on hover."""
    return {c["props"]["title"] for c in components(node) if c["props"].get("className") == "fact-key"}


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
    assert panels.AI_WRITTEN == overlay.AI_WRITTEN_LABEL["en"] and panels.AI_WRITTEN in text and "machine-translated" not in text  # the overlay's label
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


def test_a_recap_of_another_score_or_other_teams_is_not_shown(desk, monkeypatch):
    doc = write_recap(desk, monkeypatch)
    en = next(t for t in doc["texts"] if t["lang"] == "en")
    path = desk.root / MATCH / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    assert en["recap"] in texts_in(panels.after_the_match(MATCH, "insights", None, desk)[0])
    teams = state["teams"]
    for other in ({"score": dict(state["score"], away=state["score"]["away"] + 1)}, {"teams": {"home": teams["away"], "away": teams["home"]}}):
        path.write_text(json.dumps(dict(state, **other)), encoding="utf-8")  # a run that ended otherwise, or with other names
        assert texts_in(panels.after_the_match(MATCH, "insights", None, desk)[0]) == "Full time: no recap yet.", other
    path.write_text(json.dumps(state), encoding="utf-8")
    assert en["recap"] in texts_in(panels.after_the_match(MATCH, "insights", None, desk)[0])
    assert panels.same_match({k: v for k, v in doc.items() if k != "analysis"}, dict(state, score={}))  # a recap without its analysis is taken as it is


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


def test_the_shot_map_picker_offers_teams_and_halves(demo_feed):
    teams = matchfeed.team_names(demo_feed["info"])
    items = panels.graphic_items(demo_feed, "shots")
    assert [o["value"] for o in items] == ["all", "home", "away", "h1", "h2"] and [o["label"] for o in items][:3] == ["Both teams", teams["home"], teams["away"]]
    assert panels.graphic_figure(demo_feed, "shots", "away").layout.meta["filter"] == {"team": "away", "half": None}
    assert panels.graphic_figure(demo_feed, "shots", "h2").layout.meta["filter"] == {"team": None, "half": 2}
    assert panels.graphic_figure(demo_feed, "shots", None).layout.meta["filter"] == {"team": None, "half": None}


def test_the_momentum_picker_sets_the_slice(demo_feed):
    items = panels.graphic_items(demo_feed, "momentum")
    assert [o["value"] for o in items] == ["slice:5", "slice:3", "slice:1"] and items[0]["label"] == "5-minute bars"
    assert [panels.graphic_figure(demo_feed, "momentum", v).layout.meta["minutes"] for v in ("slice:1", "slice:3", None)] == [1, 3, 5]


def test_the_former_pressing_figure_is_named_for_what_it_counts(demo_feed):
    fig = graphics.pressing_figure(demo_feed)
    assert fig.layout.title.text.startswith("<b>Opponents near the ball</b>") and f"within {tactics.PRESS_RADIUS_M:g} m of the opponent on the ball" in fig.layout.title.text
    assert "Pressing" not in fig.layout.title.text
    labels = {o["value"]: o["label"] for o in panels.graphic_items(demo_feed, "tactics")}
    assert labels["press"] == "Opponents near the ball over time"
    assert panels.graphic_figure(demo_feed, "tactics", "press").layout.title.text == fig.layout.title.text
    team = matchfeed.team_names(demo_feed["info"])["home"]
    titles = {way: panels.insight_title({"figure": {"kind": "pressing", "team": "home"}, "headline_facts": {"team_name": team, "pressed": way}}) for way in ("harder", "less")}
    assert titles == {"harder": f"{team}: more players near the opponent on the ball after the break", "less": f"{team}: fewer players near the opponent on the ball after the break"}


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
                                                               "texts": [{"lang": "en", "headline": "Fourth against sixth", "why": "A top-four duel."}],
                                                               "duo": [{"speaker": "joss", "lang": "en", "line": "A top-four duel tonight.", "claims": []}]}), encoding="utf-8")
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


def test_a_visitor_sees_the_graphics_without_the_offside_margins(public, monkeypatch):
    feed = public.feed(MATCH)
    item, margin = official_run(feed)
    values = {**ON_GRAPHICS, "c-g-kind.value": "tactics", "c-g-item.value": item}
    seen = {who: dash_post("c-g-figure.figure", values, "c-g-item.value", headers) for who, headers in (("visitor", {}), ("producer", PRODUCER))}
    labels = {who: {o["value"]: o["label"] for o in out["c-g-item"]["options"]} for who, out in seen.items()}
    runs = [v for v in labels["producer"] if v.startswith("run:")]
    assert runs and labels["visitor"] == labels["producer"]  # the same items and labels, every run listed
    for v in runs:  # a label cannot be folded: never the margin, for the producer either
        assert " m beyond" not in labels["producer"][v] and " m inside" not in labels["producer"][v] and labels["producer"][v].endswith(" the line")
    assert margins_in(seen["producer"]["c-g-figure"]["figure"]) == margins_in(seen["visitor"]["c-g-figure"]["figure"]) == []
    for who in ("visitor", "producer"):
        fig = seen[who]["c-g-figure"]["figure"]
        assert fig["layout"]["meta"]["offside"]["line_x"] and "beyond the second-last defender" in fig["layout"]["title"]["text"]  # the line and the call stay
    shown = f"{abs(margin):.2f} m"
    producer_note, visitor_note = seen["producer"]["c-g-note"]["children"], seen["visitor"]["c-g-note"]["children"]
    assert shown in folded_words(producer_note) and shown not in unfolded_words(producer_note)  # the producer's margin, folded
    folded = [c for c in components(producer_note) if c["type"] == "Details"]
    assert len(folded) == 1 and not folded[0]["props"].get("open")  # closed by default
    assert shown not in str(visitor_note) and "Details" not in str(visitor_note)
    shots = dash_post("c-g-figure.figure", {**ON_GRAPHICS, "c-g-kind.value": "shots"}, "c-g-kind.value")
    assert shots["c-g-figure"]["figure"]["data"]  # every other graphic as the producer sees it
    monkeypatch.setenv("PUBLIC_MODE", "0")  # locally, the local producer gets the folded margin too
    local = dash_post("c-g-figure.figure", values, "c-g-item.value")
    assert margins_in(local["c-g-figure"]["figure"]) == [] and shown in folded_words(local["c-g-note"]["children"])


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
    assert margins_in(seen["producer", run["key"]]["c-i-figure"]["figure"]) == margins_in(seen["visitor", run["key"]]["c-i-figure"]["figure"]) == []  # never on the figure
    margin = f"{run['headline_facts']['margin_m']}"
    producer_detail = seen["producer", run["key"]]["c-i-detail"]
    assert margin in folded_words(producer_detail) and f"{margin} m" not in unfolded_words(producer_detail)  # the producer's margin, folded with the facts
    for shown in (words(seen["visitor", run["key"]]["c-i-detail"]), words(seen["visitor", run["key"]]["c-s-detail"])):  # the duo's lines: proposed text, signed by persona
        assert "Suggested lines" in shown and "never voiced" in shown and "Commentary" not in shown, shown
    assert f"{PERSONAS['wren'].short}: " in words(seen["visitor", run["key"]]["c-i-detail"]) and "wren: " not in words(seen["visitor", run["key"]]["c-i-detail"])
    monkeypatch.setenv("PUBLIC_MODE", "0")  # locally, the local producer has every control and every figure
    local = dash_post("c-r-detail.children", {**ON_INSIGHTS, "c-i-list.value": rare["id"]}, "c-i-list.value")
    assert [local[b]["disabled"] for b in ("c-i-approve", "c-i-reject", "c-s-approve", "c-s-reject")] == [False] * 4
    assert feedback_ids(local["c-i-detail"]) and fact_keys(local["c-i-detail"]) == set(rare["headline_facts"])


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
    assert "On air, by Producer" in words(out["c-i-message"]) and PRODUCER[security.PRINCIPAL_HEADER] not in words(out["c-i-message"]) and out["c-i-rev"]["data"] == 1
    entry = next(e for e in reversed(public.audit(MATCH)) if e.get("card_id") == docs[0]["id"])
    assert entry["action"] == "approved" and entry["by"] == PRODUCER[security.PRINCIPAL_HEADER]  # the audit keeps the identity
    assert {d["id"]: d for d in public.insights(MATCH)}[docs[0]["id"]]["status"] == "approved"


def test_insights_read_as_words_and_fold_what_stays_with_the_producer():
    doc = {"id": "insight-x-ht", "type": "insight", "key": "ht-lead-lost-away-2", "group": "ht-lead-lost-away", "kind": "rarity", "status": "pending", "value": 80, "minute_label": "70'",
           "headline_facts": {"team_name": "Away side", "lead_at_ht": 2, "one_in": 101, "history_count": 94, "history_opportunities": 9579},
           "field_meanings": {"lead_at_ht": "goals ahead at half-time"}, "rarity": {"basis": "leads of two lost: 94 in 9,579", "surprisal_bits": 6.66},
           "machine_translated": ["es"], "texts": [{"lang": "en", "headline": "", "why": "Two up at the break."}, {"lang": "es", "headline": "", "why": "Dos arriba."}]}
    options = panels.insight_options([dict(doc, texts=[])])
    assert texts_in(options[0]["label"].children[0].children[1]) == "Half-time lead lost" and texts_in(options[0]["label"].children[1]) == "Half-time lead lost"
    assert panels.insight_title({"key": "run-x-v2-a01178"}) == "Run behind the line" and panels.insight_title({"kind": "streak"}) == "Streak"
    run = {"figure": {"kind": "run"}, "headline_facts": {"runner_name": "Runner", "margin_m": 0.37, "runner_beyond_line": True}}
    assert panels.insight_title(run) == "Runner: beyond the line"  # never the margin in a title, for the producer either
    producer, _ = panels.insight_detail(doc, None, True, {}, read_only=False)
    visitor, _ = panels.insight_detail(doc, None, True, None, read_only=True)
    keys = lambda node: {c.title: texts_in(c) for c in find(node, lambda c: getattr(c, "className", "") == "fact-key")}  # noqa: E731
    assert keys(producer) == {k: labels.field_label(k) for k in doc["headline_facts"]} and keys(producer)["lead_at_ht"] == "Lead at half-time"
    assert set(keys(visitor)) == set(doc["headline_facts"]) - panels.PRODUCER_ONLY_FACTS
    folded = find(producer, lambda c: getattr(c, "className", "") == "producer-details")
    assert len(folded) == 1 and folded[0].open is False and texts_in(folded[0].children[0]) == panels.PRODUCER_DETAILS
    assert set(keys(folded[0])) == set(doc["headline_facts"]) & panels.PRODUCER_ONLY_FACTS and "Rarity: leads of two lost: 94 in 9,579 (6.66 bits)" in texts_in(folded[0])
    outside = texts_in([c for c in producer.children if c is not folded[0]])
    assert "9,579" not in outside and "9579" not in outside and "Half-time lead lost, rarity, priority 80" in outside
    assert not find(visitor, lambda c: getattr(c, "className", "") == "producer-details") and panels.PRODUCER_ONLY_NOTE in texts_in(visitor) and "9,579" not in texts_in(visitor)
    assert panels.AI_WRITTEN in texts_in(producer) and "machine-translated" not in texts_in(producer)
    assert [panels.AI_WRITTEN in texts_in(b) for b in panels.texts_block(doc)] == [False, True]
    assert panels.producer_details([None]) is None


def test_the_labels_live_in_a_module_both_tabs_import():
    code = "import json, sys, web.panels; print(json.dumps(['web.console' in sys.modules, web.panels.fact_label('xg-ft-home-5')]))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}).stdout
    assert json.loads(out) == [False, "Full-time xG"]  # the panels never import the console
    from web import console
    assert console.fact_label is labels.fact_label and console.field_label is labels.field_label


# --- requests per tick ------------------------------------------------------------------------------

def test_the_desk_tick_sends_the_panels_no_request():
    """The match list sets the picker on every desk tick; the panels take the copy kept in the browser, written only when the match changes."""
    picker, copy = {"id": "c-match", "property": "value"}, {"id": "c-p-match", "property": "data"}
    clientside = next(c for c in app._callback_list if c["output"] == "c-p-match.data")
    assert clientside["clientside_function"] and clientside["inputs"] == [picker]  # run in the browser: no request
    for output in ("c-g-figure.figure", "c-i-detail.children"):
        spec = next(v for k, v in app.callback_map.items() if output in k.strip(".").split("..."))
        assert picker not in spec["inputs"] and copy in spec["inputs"], output
    assert "c-p-match" in [getattr(c, "id", None) for c in panels.graphics_pane().children]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_copy_of_the_picker_changes_only_with_the_match():
    script = (f"const window = {{dash_clientside: {{no_update: 'no_update'}}}}; const same = {panels.SAME_MATCH};"
              "console.log(JSON.stringify([same('m1', 'm1'), same('m2', 'm1'), same(undefined, undefined), same(null, undefined), same('m1', undefined), same(null, 'm1')]));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=60).stdout
    assert json.loads(out) == ["no_update", "m2", "no_update", "no_update", "m1", None]
