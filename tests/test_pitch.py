import json
import re
import socket
from pathlib import Path

import pytest

pytest.importorskip("plotly")

from web import pitch  # noqa: E402
from web.pitch_export import export, load, match_actions  # noqa: E402

REPLAY = Path(__file__).resolve().parents[1] / "data" / "replay" / "demo-0007.jsonl.gz"
TRUTH = REPLAY.with_name("demo-0007.truth.json")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(scope="module")
def match():
    actions, info = match_actions(load(REPLAY))
    return actions, info, json.loads(TRUTH.read_text(encoding="utf-8"))


def on_pitch(fig) -> None:
    for trace in fig.data:
        for x, y in zip(trace.x or [], trace.y or []):
            assert 0 <= x <= pitch.LENGTH and 0 <= y <= pitch.WIDTH, (trace.name, x, y)


def test_pitch_figure_has_a_fixed_aspect_ratio():
    for theme in ("dark", "light"):
        fig = pitch.pitch_figure(theme)
        assert fig.layout.yaxis.scaleanchor == "x" and fig.layout.yaxis.scaleratio == 1
        assert len(fig.layout.shapes) > 15


def test_a_frame_builds_for_every_action_kind(match):
    actions, info, _ = match
    kinds = {}
    for a in actions:
        kinds.setdefault((a["action_type"], a["result"]), a)
    assert len(kinds) > 20
    for a in kinds.values():
        fig = pitch.frame_figure(a, "Rovers", "United", theme="light")
        players = fig.data[pitch.TRACE_HOME].x + fig.data[pitch.TRACE_AWAY].x
        assert len(players) == len(a["freeze_frame"])
        assert len(fig.data[pitch.TRACE_ACTOR].x) == 1
        assert a["minute_label"] in fig.layout.title.text and f"{a['score']['home']}-{a['score']['away']}" in fig.layout.title.text
        on_pitch(fig)


def test_every_frame_stays_on_the_pitch_in_both_views(match):
    actions, _, _ = match
    for a in actions:
        for view in (None, "home", "away"):
            traces = pitch.frame_traces(a, view=view)
            for t in traces:
                for x, y in zip(t.x or [], t.y or []):
                    assert 0 <= x <= pitch.LENGTH and 0 <= y <= pitch.WIDTH


def test_flipping_the_view_mirrors_the_players(match):
    actions, _, _ = match
    a = next(a for a in actions if a["team"] == "away")
    same, flipped = pitch.frame_traces(a, view="away"), pitch.frame_traces(a, view="home")
    for k in (pitch.TRACE_HOME, pitch.TRACE_AWAY):
        for x0, y0, x1, y1 in zip(same[k].x, same[k].y, flipped[k].x, flipped[k].y):
            assert x1 == pytest.approx(pitch.LENGTH - x0) and y1 == pytest.approx(pitch.WIDTH - y0)


@pytest.mark.parametrize("zoom", [None, 7])
def test_offside_margin_matches_the_frame(match, zoom):
    actions, _, truth = match
    by_id = {a["event_id"]: a for a in actions}
    assert truth["offsides"]
    for o in truth["offsides"]:
        pass_action = by_id[o["event_id"]]
        fig = pitch.offside_figure(pass_action, True, "Rovers", "United", zoom=zoom)
        meta = fig.layout.meta["offside"]
        frame = pass_action["freeze_frame"]
        defenders = sorted((p["location"][0] for p in frame if not p["teammate"]), reverse=True)
        runner = next(p for p in frame if p["player_id"] == o["player_id"])
        geometric = runner["location"][0] - defenders[1]
        text = next(a.text for a in fig.layout.annotations if a.name == "margin")
        annotated = float(re.search(r"(-?\d+\.\d+) m", text).group(1))
        assert meta["runner_id"] == o["player_id"] and meta["line_x"] == defenders[1]
        if "offside" in pass_action:  # set by the VAR correction: the feed's own figure is shown
            assert annotated == pytest.approx(pass_action["offside"]["margin_m"])
        assert abs(annotated - o["margin_m"]) <= 0.05 + 1e-9
        assert abs(annotated - geometric) <= 0.05 + 1e-9  # the frame rounds locations to 0.1 m
        assert any(s.type == "line" and s.x0 == s.x1 == defenders[1] for s in fig.layout.shapes)
        on_pitch(fig)


def test_offside_of_the_63rd_minute_without_the_correction(match):
    actions, _, truth = match
    goal = next(m for m in truth["moments"] if m["key"] == "goal-63")
    pass_action = next(a for a in actions if a["event_id"] in goal["affected_event_ids"] and a["action_type"] == "pass")
    raw = {k: v for k, v in pass_action.items() if k != "offside"}  # the version on air before VAR
    g = pitch.offside_geometry(raw)
    assert g["runner_id"] == "home-20" and not g["official"] and g["margin_m"] == pytest.approx(0.2)
    assert "0.25 m" in pitch.offside_figure(pass_action, home_name="Rovers", away_name="United").layout.title.text


def test_sequence_frames_animate_the_buildup(match):
    actions, _, truth = match
    goal = next(m for m in truth["moments"] if m["key"] == "goal-63")
    seq = pitch.buildup(actions, goal["event_id"])
    assert 5 <= len(seq) <= 8 and seq[-1]["event_id"] == goal["event_id"]
    frames = pitch.sequence_frames(seq, "Rovers", "United")
    assert len(frames) == len(seq) and all(len(f.data) == 5 for f in frames)
    ids = [set(f.data[pitch.TRACE_HOME].ids) for f in frames]
    assert all(i == ids[0] for i in ids)
    fig = pitch.sequence_figure(seq, "Rovers", "United")
    assert len(fig.frames) == len(seq) and fig.layout.sliders[0].steps


def test_export_writes_html(match, tmp_path):
    written = export(REPLAY, "goal-63", tmp_path)
    names = {p.name for p in written}
    assert {"goal-63-offside.html", "goal-63-offside-zoom.html", "goal-63-buildup.html"} <= names
    assert all(p.stat().st_size < 200_000 for p in written if p.suffix == ".html")
