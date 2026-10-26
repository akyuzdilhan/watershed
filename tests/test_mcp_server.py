"""The desk's read-only MCP server (worker.mcp_server): every tool through an in-process mcp.Client, the stdio command over a desk folder, and the fact functions on their own. No network."""

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from generator import generate_match
from league.history import generate
from worker import storyline as sl
from worker.desk import Desk
from worker.mcp_server import DESK_TOOLS, HISTORY_TOOLS, LAST_TIME_KINDS, FactRegistry, HistoryFacts, build_server, match_state_facts, storyline_facts
from worker.pipeline import JsonFileSink, Pipeline
from worker.writer import TemplateWriter, card_facts, insight_facts

TEAMS = {"home": "Varleigh", "away": "Yarnford"}
SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(scope="module")
def history():
    return generate(9, 5)


@pytest.fixture(scope="module")
def hf(history):
    return HistoryFacts.build(history)


def session(server, *calls):
    """Opens one in-process client, lists the tools, then makes each (tool, arguments) call."""
    from mcp import Client

    async def go():
        async with Client(server, read_timeout_seconds=5) as client:
            tools = (await client.list_tools()).tools
            return tools, [await client.call_tool(name, args) for name, args in calls]

    return asyncio.run(go())


def texts(server, *calls) -> list[str]:
    return [r.content[0].text for r in session(server, *calls)[1]]


def card(id_, moment, side, minute, clock_s, player="Elio Ingleby", status="approved", facts=None, **extra):
    base = {"goal": {"team_wp_before_pct": 30, "team_wp_after_pct": 54}, "save": {"xg_pct": 20, "team_wp_before_pct": 81, "team_wp_if_goal_pct": 24},
            "red_card": {"team_wp_before_pct": 55, "team_wp_after_pct": 40}}
    return {"id": id_, "version": 1, "match_id": "m", "moment": moment, "team": side, "team_name": TEAMS[side], "minute_label": minute, "clock_s": clock_s,
            "player": {"id": f"{side}-9", "name": player}, "score_before": {"home": 1, "away": 1}, "facts": facts or base[moment], "status": status, "value_pts": 24, **extra}


# --- the tools ---------------------------------------------------------------------------------------

def test_seven_read_only_tools_with_text_results_only(hf):
    tools, _ = session(build_server(FactRegistry(), hf))
    assert sorted(t.name for t in tools) == sorted(DESK_TOOLS + HISTORY_TOOLS)
    for t in tools:
        a = t.annotations
        assert (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint) == (True, False, True, False), t.name
        assert t.output_schema is None and t.description, t.name


def test_without_history_only_the_desk_tools():
    tools, _ = session(build_server(FactRegistry()))
    assert sorted(t.name for t in tools) == sorted(DESK_TOOLS)


def test_get_moment_serves_the_writer_json_byte_for_byte():
    registry = FactRegistry()
    goal, save = card("card-g", "goal", "home", "52'", 3100.0), card("card-s", "save", "home", "72'", 4300.0)
    insight = {"id": "insight-m-sub4", "type": "insight", "match_id": "m", "kind": "record", "minute_label": "83'",
               "headline_facts": {"player": "Marijn Scarpetta", "goals_by_substitute": 4}, "field_meanings": {"goals_by_substitute": "goals by the substitute"}}
    expected = {}
    for doc, (f, m) in ((goal, card_facts(goal, TEAMS)), (save, card_facts(save, TEAMS)), (insight, insight_facts(insight, TEAMS))):
        registry.put_moment(doc["id"], f, m)
        expected[doc["id"]] = json.dumps({"facts": f, "field_meanings": m}, ensure_ascii=False)
    got = texts(build_server(registry), *[("get_moment", {"card_id": i}) for i in expected], ("get_moment", {"card_id": "nope"}))
    assert got[:3] == list(expected.values())
    assert got[3] == json.dumps({"error": "unknown card_id nope"})
    assert registry.served["get_moment"] == 4


def test_match_state_counts_the_goals_that_count_up_to_the_card():
    cards = [card("c14", "goal", "away", "14'", 840.0, "Vito Valtonen"), card("c52", "goal", "home", "52'", 3120.0, "Marijn Scarpetta"),
             card("c63", "goal", "home", "63'", 3780.0, "Marijn Scarpetta", status="retracted"),
             card("c66", "goal", "home", "66'", 3960.0, "Marijn Scarpetta", status="pending"), card("c70", "red_card", "away", "70'", 4200.0, "Hugo Wojcik")]
    facts, meanings, subject = match_state_facts(cards, cards[3], TEAMS)
    assert facts["match_score_now"] == "Varleigh 2-1 Yarnford" and facts["match_minute"] == "66'"
    assert facts["match_goal_log"] == "14' Vito Valtonen (Yarnford), 52' Marijn Scarpetta (Varleigh), 66' Marijn Scarpetta (Varleigh)"
    assert "63'" not in facts["match_goal_log"] and "70'" not in facts["match_goal_log"]
    assert (facts["match_varleigh_goals"], facts["match_yarnford_goals"]) == (2, 1)
    assert (facts["match_varleigh_largest_deficit"], facts["match_yarnford_largest_deficit"]) == (1, 1)
    assert facts["match_varleigh_red_cards"] == facts["match_yarnford_red_cards"] == 0
    assert set(meanings) == set(facts) and subject == ["Varleigh", "Yarnford"]
    early, _, _ = match_state_facts(cards, card("c10", "save", "home", "10'", 600.0), TEAMS)
    assert early["match_goal_log"] == "no goal yet" and early["match_score_now"] == "Varleigh 0-0 Yarnford"


def test_get_match_state_through_the_server():
    registry = FactRegistry()
    cards = [card("c14", "goal", "away", "14'", 840.0, "Vito Valtonen"), card("c52", "goal", "home", "52'", 3120.0, "Marijn Scarpetta")]
    registry.put_state("c52", *match_state_facts(cards, cards[1], TEAMS))
    state, missing = texts(build_server(registry), ("get_match_state", {"card_id": "c52"}), ("get_match_state", {"card_id": "c99"}))
    assert json.loads(state)["facts"]["match_score_now"] == "Varleigh 1-1 Yarnford" and json.loads(state)["subject"] == ["Varleigh", "Yarnford"]
    assert json.loads(missing) == {"error": "unknown card_id c99"}
    assert registry.served["get_match_state"] == 2


def test_storyline_facts_keep_angles_and_stakes_in_percent_only(history):
    doc = sl.build_storyline(9, 30, history, "demo-0007", n_sims=1000)
    facts, meanings, subject = storyline_facts(doc)
    assert {"story_thread1_angle", "story_varleigh_rank", "story_varleigh_relegation_chance_pct", "history_through"} <= set(facts)
    assert "story_varleigh_points" not in facts and "story_varleigh_relegation_stake_pts" not in facts and "story_points_gap" not in facts
    assert all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in facts.values())
    assert "story_h2h_last_meetings" not in facts  # a list
    assert set(meanings) == set(facts) and subject == ["Varleigh", "Yarnford"]
    registry = FactRegistry()
    registry.put_storyline("demo-0007", facts, meanings, subject)
    served, missing = texts(build_server(registry), ("get_storyline", {"match_id": "demo-0007"}), ("get_storyline", {"match_id": "other"}))
    assert json.loads(served)["facts"]["story_thread1_angle"] == doc["threads"][0]["angle"]
    assert json.loads(missing) == {"error": "no storyline for match other"}


# --- the history tools -----------------------------------------------------------------------------------

def test_player_history_is_the_history_record(history, hf):
    registry = FactRegistry()
    (pid,) = history.find_players("Marijn Scarpetta")
    p = history.player(pid)
    known, unknown = texts(build_server(registry, hf), ("player_history", {"name": "Marijn Scarpetta"}), ("player_history", {"name": "Nobody Atall"}))
    out = json.loads(known)
    f = out["facts"]
    assert (f["player_marijn_scarpetta_league_goals"], f["player_marijn_scarpetta_appearances"], f["player_marijn_scarpetta_sub_appearances"]) == (p["goals"], p["appearances"], p["sub_appearances"])
    assert f["player_marijn_scarpetta_first_season"] == p["clubs"][0]["season"] and f["history_through"] == history.seasons()[-1]
    assert "not counted" in out["field_meanings"]["player_marijn_scarpetta_league_goals"]
    assert out["subject"][0] == "Marijn Scarpetta" and "Varleigh" in out["subject"]
    assert json.loads(unknown) == {"error": "unknown player Nobody Atall"}
    assert registry.served["player_history"] == 2


@pytest.mark.parametrize("club", [None, "Varleigh"])
def test_last_time_is_told_in_seasons_never_in_matches(hf, club):
    calls = [("last_time", {"kind": k} | ({"club": club} if club else {})) for k in LAST_TIME_KINDS]
    for kind, raw in zip(LAST_TIME_KINDS, texts(build_server(FactRegistry(), hf), *calls)):
        out = json.loads(raw)
        f = out["facts"]
        key = kind if club is None else f"{kind}_varleigh"
        numbers = [v for v in f.values() if isinstance(v, (int, float))]
        assert all(isinstance(v, int) and v >= 0 for v in numbers), f
        assert not [v for v in numbers if 0 < v < 1]
        assert not [k for k in f if "matches" in k]
        assert f[f"{key}_count"] >= f[f"{key}_seasons_with_one"]
        if f[f"{key}_count"]:
            assert f[f"last_{key}_seasons_ago"] == hf.view.current_idx - hf.view.seasons.index(f[f"last_{key}_season"])
        else:
            assert f[f"last_{key}_season"] == "never in the history"
        if club:
            assert out["subject"] == [club]
        else:
            assert out["subject"] == [s for s in (f.get(f"last_{key}_player"), f.get(f"last_{key}_club")) if s]


def test_a_club_count_is_at_most_the_league_count(hf):
    for kind in LAST_TIME_KINDS:
        league, _, _ = hf.last_time(kind)
        varleigh, _, _ = hf.last_time(kind, "Varleigh")
        assert varleigh[f"{kind}_varleigh_count"] <= league[f"{kind}_count"]


def test_bad_arguments_are_refused_by_the_schema(hf):
    _, results = session(build_server(FactRegistry(), hf), ("last_time", {"kind": "own_goal"}), ("club_history", {"club": "x" * 61}),
                         ("last_time", {"kind": "hat_trick", "club": "Nowhere Town"}))
    assert results[0].is_error and results[1].is_error
    assert not results[2].is_error and json.loads(results[2].content[0].text) == {"error": "unknown club Nowhere Town"}


def test_head_to_head_reads_the_same_both_ways_and_club_history_is_the_record(history, hf):
    ab, ba, club, again = texts(build_server(FactRegistry(), hf), ("head_to_head", {"club_a": "Varleigh", "club_b": "Yarnford"}),
                                ("head_to_head", {"club_a": "Yarnford", "club_b": "Varleigh"}), ("club_history", {"club": "Varleigh"}), ("club_history", {"club": "Varleigh"}))
    assert ab == ba
    h2h = sl.head_to_head(history, "Varleigh", "Yarnford")
    f = json.loads(ab)["facts"]
    assert f["h2h_varleigh_yarnford_meetings"] == h2h["meetings"]
    assert f["h2h_varleigh_yarnford_varleigh_wins"] + f["h2h_varleigh_yarnford_yarnford_wins"] + f["h2h_varleigh_yarnford_draws"] == h2h["meetings"]
    record = history.club("Varleigh")
    c = json.loads(club)["facts"]
    assert (c["club_varleigh_titles"], c["club_varleigh_seasons_in_league"], c["club_varleigh_founded_season"]) == (record["titles"], record["seasons_in_league"], record["founded_season"])
    assert c.get("club_varleigh_best_finish") == record["best_finish"]
    assert club == again


def test_same_arguments_same_text(hf):
    calls = [("player_history", {"name": "Marijn Scarpetta"}), ("last_time", {"kind": "brace_by_substitute", "club": "Varleigh"})]
    assert texts(build_server(FactRegistry(), hf), *calls) == texts(build_server(FactRegistry(), hf), *calls)


def test_the_registry_under_four_threads():
    registry = FactRegistry()
    errors = []

    def work(i):
        for k in range(300):
            registry.put_moment(f"c{i}-{k}", {"n": k}, {"n": "a number"})
            registry.put_state(f"c{i}-{k}", {"match_minute": f"{k}'"}, {}, ["Varleigh", "Yarnford"])
            if json.loads(registry.moment(f"c{i}-{k}"))["facts"]["n"] != k or not registry.state(f"c{i}-{k}"):
                errors.append((i, k))
            registry.mark("get_moment")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and registry.served["get_moment"] == 1200


# --- stdio, for any MCP host ------------------------------------------------------------------------------

def test_the_desk_folder_reads_nothing_outside_it(tmp_path):
    from worker.mcp_server import DeskDirSource

    story = {"home": "Varleigh", "away": "Yarnford", "threads": [{"angle": "a derby", "kind": "rivalry", "facts": {"meetings": 12}, "field_meanings": {"meetings": "meetings"}}]}
    for folder in ("desk/demo", "outside"):
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "storyline.json").write_text(json.dumps(story), encoding="utf-8")
    source = DeskDirSource(tmp_path / "desk")
    assert json.loads(source.storyline("demo"))["facts"]["story_meetings"] == 12
    for match_id in ("../outside", "..\\outside", str(tmp_path / "outside"), "..", ".", ""):
        assert source.storyline(match_id) is None, match_id

def test_the_stdio_command_serves_a_desk_folder(tmp_path, history):
    from mcp import Client, StdioServerParameters

    stream, _ = generate_match(7, "demo")
    sink = JsonFileSink(tmp_path / "desk")
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True))
    for m in stream:
        pipeline.handle(m)
    pipeline.flush()
    folder = tmp_path / "desk" / "demo-0007"
    cards = json.loads((folder / "cards.json").read_text(encoding="utf-8"))
    teams = json.loads((folder / "state.json").read_text(encoding="utf-8"))["teams"]
    card = next(c for c in cards if c["minute_label"] == "66'")
    expected = json.dumps(dict(zip(("facts", "field_meanings"), card_facts(card, teams))), ensure_ascii=False)
    history_dir = history.save(tmp_path / "history")
    params = StdioServerParameters(command=sys.executable, args=["-m", "worker.mcp_server", "--desk-dir", str(tmp_path / "desk"), "--history", str(history_dir)],
                                   env={**os.environ, "PYTHONPATH": str(SRC)})

    async def go():
        async with Client(params, read_timeout_seconds=60) as client:
            tools = (await client.list_tools()).tools
            calls = [await client.call_tool("get_moment", {"card_id": card["id"]}), await client.call_tool("get_match_state", {"card_id": card["id"]}),
                     await client.call_tool("player_history", {"name": "Marijn Scarpetta"})]
            return tools, [c.content[0].text for c in calls]

    tools, (moment, state, player) = asyncio.run(go())
    assert sorted(t.name for t in tools) == sorted(DESK_TOOLS + HISTORY_TOOLS)
    assert all(t.annotations.read_only_hint for t in tools)
    assert moment == expected
    assert json.loads(state)["facts"]["match_score_now"] == "Varleigh 3-2 Yarnford"  # the 63' goal was ruled out
    assert json.loads(player)["facts"]["player_marijn_scarpetta_league_goals"] == history.player(history.find_players("Marijn Scarpetta")[0])["goals"]
