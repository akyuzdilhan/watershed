import json
import math
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


# --- match graphics ------------------------------------------------------------------------------------

from generator import generate_match  # noqa: E402
from web import graphics, matchfeed  # noqa: E402
from worker import tactics  # noqa: E402


@pytest.fixture(scope="module")
def demo_feed():
    return matchfeed.build(load(REPLAY))


def annotation_arrows(fig) -> list:
    return [a for a in fig.layout.annotations if a.showarrow and a.axref == "x"]


def test_shot_map_puts_every_shot_in_its_outcome_and_sets_voided_goals_apart(demo_feed):
    fig = graphics.shot_map(demo_feed)
    by_name = {t.name: t for t in fig.data}
    assert sum(len(t.x) for t in fig.data) == len(graphics.shots(demo_feed)) == 22
    assert len(by_name["Goal ruled out (VAR)"].x) == 2 and len(by_name["Goal"].x) == 7
    xgs = [a["shot"]["xg"] for a in demo_feed["actions"] if tactics.counted_goal(a)]
    assert sorted(by_name["Goal"].marker.size) == pytest.approx(sorted(8 + 34 * math.sqrt(xg) for xg in xgs))
    on_pitch(fig)


def test_buildup_arrows_widen_with_the_threat_added(demo_feed):
    for m in graphics.moments(demo_feed):
        fig = graphics.buildup_figure(demo_feed, m["event_id"])
        assert fig.layout.meta["buildup"][-1] == m["event_id"]
        assert annotation_arrows(fig)
    assert graphics.arrow_width(0) == 1.5 and graphics.arrow_width(0.05) > graphics.arrow_width(0.01) and graphics.arrow_width(5) == 12
    assert "ruled out by VAR" in graphics.buildup_figure(demo_feed, "demo-0007-v2-a01179").layout.title.text


def test_set_piece_animation_runs_from_delivery_to_first_contact(demo_feed):
    corners = [a for a in demo_feed["actions"] if a["action_type"] == "corner_crossed"]
    assert corners
    for c in corners:
        fig = graphics.set_piece_figure(demo_feed, c["event_id"])
        assert fig.layout.meta["steps"][0] == c["event_id"] and len(fig.frames) >= 2
        widths = [a.arrowwidth for a in fig.frames[-1].layout.annotations]
        assert widths and widths[0] == graphics.arrow_width(tactics.location_value(c["end_x"], c["end_y"]))
    for side in ("home", "away"):
        assert graphics.set_pieces_map(demo_feed, side).layout.title.text


def test_momentum_bars_sum_the_threat_added_and_carry_the_curve(demo_feed):
    series = graphics.momentum_series(demo_feed)
    total = sum(max(tactics.threat_added(a), 0) for a in demo_feed["actions"] if a["team"] == "home")
    assert math.isclose(sum(p["threat"] for p in series["home"]), total, abs_tol=0.01)
    timeline = [{"seq": a["seq"], "event_id": a["event_id"], "clock_s": a["clock_s"], "minute_label": a["minute_label"], "home": 0.4, "draw": 0.3, "away": 0.3}
                for a in demo_feed["actions"][::50]]
    fig = graphics.momentum_figure(demo_feed, timeline)
    assert [t.type for t in fig.data] == ["bar", "bar", "scatter", "scatter", "scatter"]
    xs = list(fig.data[2].x)
    assert xs == sorted(xs)  # the second half continues after the first-half added time
    assert len(graphics.momentum_figure(demo_feed, None).data) == 2


def test_penalty_and_direct_free_kick_views():
    stream, _ = generate_match(14)  # an unscripted match with a penalty
    feed = matchfeed.build(stream)
    pen = next(a for a in feed["actions"] if a["action_type"] == "shot_penalty")
    fig = graphics.shot_on_goal_figure(feed, pen["event_id"])
    assert math.isclose(fig.layout.meta["ball_h"], (pitch.WIDTH - pen["end_y"]) - pitch.MID_Y, abs_tol=0.01)
    assert "Penalty" in fig.layout.title.text
    fk = next(a for a in feed["actions"] if a["action_type"] == "shot_freekick")
    assert "Free kick" in graphics.shot_on_goal_figure(feed, fk["event_id"]).layout.title.text


def test_tactical_figures_show_the_measured_numbers(demo_feed):
    assert len(graphics.line_break_figure(demo_feed, "demo-0007-v2-a01454").layout.meta["bypassed_ids"]) == 7
    assert graphics.run_figure(demo_feed, "demo-0007-v2-a01178", "home-20").layout.meta["offside"]["margin_m"] == 0.25
    reception = tactics.space_at_reception(demo_feed["actions"])[0]
    assert graphics.space_figure(demo_feed, reception["event_id"]).layout.meta["space_m"] == reception["space_m"]
    assert len(graphics.line_height_figure(demo_feed).data) == 2 and len(graphics.pressing_figure(demo_feed).data) == 2


@pytest.mark.parametrize("seed", [2, 5, 14, 21])
def test_every_graphic_builds_on_unscripted_seeds(seed):
    stream, _ = generate_match(seed)
    feed = matchfeed.build(stream)
    figs = [graphics.shot_map(feed), graphics.momentum_figure(feed), graphics.line_height_figure(feed), graphics.pressing_figure(feed)]
    figs += [graphics.buildup_figure(feed, m["event_id"]) for m in graphics.moments(feed)]
    figs += [graphics.set_piece_figure(feed, a["event_id"]) for a in graphics.set_pieces(feed) if a["action_type"] in graphics.SET_PIECES][:5]
    figs += [graphics.shot_on_goal_figure(feed, a["event_id"]) for a in graphics.set_pieces(feed) if a["action_type"] in graphics.DIRECT]
    for d in tactics.insight_candidates(feed["actions"], feed["info"]):
        fig = graphics.insight_figure(d, feed)
        assert fig is not None, d["key"]
        figs.append(fig)
    for fig in figs:
        fig.to_json()


def test_insight_figures_on_the_demo(demo_feed):
    docs = tactics.insight_candidates(demo_feed["actions"], demo_feed["info"], "demo-0007")
    assert docs and all(graphics.insight_figure(d, demo_feed) is not None for d in docs)
    assert graphics.insight_figure({"figure": {"kind": "line_break", "event_id": "missing"}}, demo_feed) is None
