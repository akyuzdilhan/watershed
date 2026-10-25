import json
import math
import statistics
import sys
from pathlib import Path

import pytest
from dash import no_update

from generator import generate_match
from web import graphics, matchfeed, panels, store
from web.app import app
from worker import recap, tactics
from worker.desk import Desk
from worker.pipeline import JsonFileSink, Pipeline
from worker.writer import TemplateWriter

MATCH = "demo-0007"
REPLAY = Path(__file__).resolve().parents[1] / "data" / "replay" / f"{MATCH}.jsonl.gz"


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
    outputs = [{"id": part.split(".")[0], "property": part.split(".", 1)[1]} for part in key.strip(".").split("...")]
    body = {"output": key, "outputs": outputs,
            "inputs": [{"id": "c-i-tick", "property": "n_intervals", "value": 1}, {"id": "c-i-rev", "property": "data", "value": 0},
                       {"id": "c-match", "property": "value", "value": MATCH}, {"id": "c-i-list", "property": "value", "value": None},
                       {"id": "c-view", "property": "value", "value": "insights"}],
            "state": [{"id": "c-i-sig", "property": "data", "value": None}, {"id": "c-r-sig", "property": "data", "value": None}],
            "changedPropIds": ["c-i-tick.n_intervals"]}
    response = app.server.test_client().post("/_dash-update-component", json=body)
    assert response.status_code == 200
    answer = response.get_json()["response"]
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
