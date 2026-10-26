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
        fig = pitch.frame_figure(a, "Varleigh", "Yarnford", theme="light")
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
        fig = pitch.offside_figure(pass_action, True, "Varleigh", "Yarnford", zoom=zoom)
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
    assert "0.25 m" in pitch.offside_figure(pass_action, home_name="Varleigh", away_name="Yarnford").layout.title.text


def test_sequence_frames_animate_the_buildup(match):
    actions, _, truth = match
    goal = next(m for m in truth["moments"] if m["key"] == "goal-63")
    seq = pitch.buildup(actions, goal["event_id"])
    assert 5 <= len(seq) <= 8 and seq[-1]["event_id"] == goal["event_id"]
    frames = pitch.sequence_frames(seq, "Varleigh", "Yarnford")
    assert len(frames) == len(seq) and all(len(f.data) == 5 for f in frames)
    ids = [set(f.data[pitch.TRACE_HOME].ids) for f in frames]
    assert all(i == ids[0] for i in ids)
    fig = pitch.sequence_figure(seq, "Varleigh", "Yarnford")
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
    outcomes = [t for t in fig.data if t.name != "xG key"]
    assert sum(len(t.x) for t in outcomes) == len(graphics.shots(demo_feed)) == 22
    assert len(by_name["Goal ruled out (VAR)"].x) == 2 and len(by_name["Goal"].x) == 7
    xgs = [a["shot"]["xg"] for a in demo_feed["actions"] if tactics.counted_goal(a)]
    assert sorted(by_name["Goal"].marker.size) == pytest.approx(sorted(8 + 34 * math.sqrt(xg) for xg in xgs))
    for trace in outcomes:
        assert 0 <= min(trace.x) and max(trace.x) <= pitch.LENGTH and 0 <= min(trace.y) and max(trace.y) <= pitch.WIDTH, trace.name
    key = by_name["xG key"]  # the size key under the pitch, drawn with the shots' own scale
    assert list(key.marker.size) == [graphics.shot_size(v) for v in graphics.XG_KEY] and all(y < 0 for y in key.y) and key.showlegend is False


def test_shot_map_filters_by_team_and_half(demo_feed):
    every = graphics.shots(demo_feed)
    for team, half in (("home", None), ("away", None), (None, 1), (None, 2)):
        fig = graphics.shot_map(demo_feed, team=team, half=half)
        expected = [a for a in every if (team is None or a["team"] == team) and (half is None or (a["period"] >= 2) == (half == 2))]
        drawn = sorted(i for t in fig.data if t.name != "xG key" for i in t.ids)
        assert drawn == sorted(a["event_id"] for a in expected) and fig.layout.meta["shots"] == len(expected), (team, half)
        headers = [a.text for a in fig.layout.annotations if "shots, xG" in (a.text or "")]
        assert len(headers) == (1 if team else 2)


def test_buildup_numbers_the_actions_and_draws_each_kind_its_own_way(demo_feed):
    for m in graphics.moments(demo_feed):
        fig = graphics.buildup_figure(demo_feed, m["event_id"])
        seq = fig.layout.meta["buildup"]
        assert seq[-1] == m["event_id"] and len(fig.layout.meta["sequence"]) == len(seq)
        assert all(line.startswith(f"{k + 1}. ") for k, line in enumerate(fig.layout.meta["sequence"]))
        assert fig.layout.meta["sequence"][-1].endswith(("goal", "goal ruled out (VAR)", "saved", "post", "blocked", "off target"))
        dots = next(t for t in fig.data if t.name == "On the ball, in order")
        assert sorted(int(n) for label in dots.text for n in label.split(",")) == list(range(1, len(seq) + 1))  # every action numbered once
        strokes = [t for t in fig.data if (t.meta or {}).get("kind")]
        assert strokes and {t.meta["kind"] for t in strokes} <= {"pass", "carry", "cross", "shot"}
        for t in strokes:
            assert t.line.dash == graphics.STROKES[t.meta["kind"]][1] and t.marker.symbol[-1] == "arrow"
        shot = next(t for t in strokes if t.meta["kind"] == "shot")
        assert shot.line.width >= 5 and shot.line.color == graphics.OUTCOMES["voided" if m.get("voided") else m["shot"]["outcome"]][1]
        caption = next(a for a in fig.layout.annotations if a.name == "sequence")
        assert "1. " in caption.text and next(a for a in fig.layout.annotations if a.name == "won").text
    assert graphics.arrow_width(0) == 1.5 and graphics.arrow_width(0.05) > graphics.arrow_width(0.01) and graphics.arrow_width(5) == 12
    assert "ruled out by VAR" in graphics.buildup_figure(demo_feed, "demo-0007-v2-a01179").layout.title.text
    assert {graphics.stroke({"action_type": k}) for k in ("dribble", "take_on")} == {"carry"} and graphics.stroke({"action_type": "cross"}) == "cross"
    assert graphics.stroke({"action_type": "tackle"}) == "won" and graphics.stroke({"action_type": "throw_in"}) == "pass"


def test_buildup_says_where_and_how_the_ball_was_won(demo_feed):
    fig = graphics.buildup_figure(demo_feed, "demo-0007-v2-a01179")  # 63': won by an interception deep in its own half
    won = fig.layout.meta["won"]
    assert won["how"] == "interception" and not won["high"] and won["metres"] < 20
    assert fig.layout.meta["sequence"][0].endswith("wins it (interception)")
    assert "flagged offside" in fig.layout.meta["sequence"][-2]
    label = next(a for a in fig.layout.annotations if a.name == "won").text
    assert label.startswith("<b>Won here</b>: interception") and "m from own goal" in label
    feed = {"info": demo_feed["info"], "actions": [dict(a) for a in demo_feed["actions"]]}
    first = next(a for a in feed["actions"] if a["event_id"] == won["event_id"])
    first["start_x"] = 70.0  # the same interception in the opponent's half
    fig = graphics.buildup_figure(feed, "demo-0007-v2-a01179")
    assert fig.layout.meta["won"]["high"] and "(high regain)" in next(a for a in fig.layout.annotations if a.name == "won").text
    assert next(t for t in fig.data if t.name == "Ball won high").marker.symbol == "star"


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
    assert [t.type for t in graphics.momentum_figure(demo_feed, None).data] == ["bar", "bar", "scatter", "scatter"]  # bars and the rolling averages


def test_momentum_marks_goals_var_and_red_cards_and_takes_the_slice(demo_feed):
    marks = graphics.match_marks(demo_feed)
    assert [m["kind"] for m in marks].count("goal") == 7 and [m["kind"] for m in marks].count("voided") == 2 and [m["kind"] for m in marks].count("red") == 1
    labels = [m["label"] for m in marks]
    assert "1-2 52'" in labels and "5-2 88'" in labels and "× 63' VAR" in labels and "78' red card" in labels
    fig = graphics.momentum_figure(demo_feed, None, minutes=3)
    dashes = [s.line.dash for s in fig.layout.shapes if s.name in ("goal", "voided", "red")]
    assert dashes.count("solid") == 7 and dashes.count("dash") == 2 and dashes.count("dot") == 1  # told apart by the dash, not only the colour
    texts = [a.text for a in fig.layout.annotations]
    assert "<b>1-2 52'</b>" in texts and "HT" in texts
    goal_colors = {s.line.color for s in fig.layout.shapes if s.name == "goal"}
    assert goal_colors == {pitch.THEMES["dark"]["home"], pitch.THEMES["dark"]["away"]}  # the scorer's colour
    assert fig.layout.meta["minutes"] == 3 and fig.layout.meta["window"] == 5 and fig.data[0].width == pytest.approx(3 * 0.85)
    roll = graphics.rolling_threat(graphics.momentum_series(demo_feed, 5)["home"], 5, 0.0, 3)
    first = [p["threat"] for p in graphics.momentum_series(demo_feed, 5)["home"] if p["period"] == 1][:2]
    assert roll[0][1] == pytest.approx(sum(first) / 2, abs=1e-3)  # centred window, cut at the start of the period
    assert graphics.label_rows([10, 12, 30, 31, 33], 7.5) == [0, 1, 0, 1, 2]


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
