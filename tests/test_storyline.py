import json

import pytest

from league.season import League, match_importance
from worker import storyline as sl

SIMS = 1000  # enough for ordered chances on seed 9, fast enough for the suite


class FakeHistory:
    """The read API of `league.history.History` (docs/contracts.md), on a handful of matches."""

    SEASONS = ["2023/24", "2024/25", "2025/26"]
    CLUBS = {"Rovers": {"name": "Rovers", "founded_season": "1976/77", "seasons_in_league": 50, "titles": 2, "best_finish": 1},
             "United": {"name": "United", "founded_season": "1976/77", "seasons_in_league": 41, "titles": 0, "best_finish": 3}}
    PLAYERS = {
        "P000001": {"id": "P000001", "name": "Aldo Brenner", "position": "FW", "born": 1998, "clubs": [{"season": "2025/26", "club": "Rovers"}],
                    "goals": 98, "appearances": 210, "sub_appearances": 12},
        "P000002": {"id": "P000002", "name": "Corin Vale", "position": "MF", "born": 1994, "clubs": [{"season": "2025/26", "club": "United"}],
                    "goals": 31, "appearances": 299, "sub_appearances": 40},
        "P000003": {"id": "P000003", "name": "Edgar Moss", "position": "FW", "born": 1999, "clubs": [{"season": "2025/26", "club": "Dunholt"}],
                    "goals": 49, "appearances": 120, "sub_appearances": 5},
    }

    def __init__(self):
        def m(season, md, home, away, hg, ag, scorers=()):
            return {"season": season, "matchday": md, "home": home, "away": away, "home_goals": hg, "away_goals": ag, "half_time": [0, 0],
                    "scorers": [{"player_id": p, "club": c, "minute": 10 + k} for k, (p, c) in enumerate(scorers)]}
        self._matches = [
            m("2023/24", 5, "Rovers", "United", 4, 0), m("2023/24", 24, "United", "Rovers", 1, 1),
            m("2024/25", 8, "United", "Rovers", 3, 1), m("2024/25", 27, "Rovers", "United", 2, 2),
            m("2025/26", 3, "Rovers", "Dunholt", 3, 1, [("P000001", "Rovers"), ("P000001", "Rovers"), ("P000003", "Rovers"), ("P000009", "Dunholt")]),
            m("2025/26", 9, "Rovers", "United", 1, 2, [("P000001", "Rovers"), ("P000002", "United"), ("P000002", "United")]),
            m("2025/26", 28, "United", "Rovers", 0, 0),
        ]

    def seasons(self):
        return list(self.SEASONS)

    def club(self, name):
        return dict(self.CLUBS[name])

    def head_to_head(self, home, away):
        pair = {home, away}
        return sorted((x for x in self._matches if {x["home"], x["away"]} == pair), key=lambda x: (x["season"], x["matchday"]), reverse=True)

    def matches(self, where):
        return [x for x in self._matches if where(x)]

    def player(self, player_id):
        return dict(self.PLAYERS[player_id])

    def count(self, event, where):
        return len(self.matches(where)) if event == "match" else 0

    def last(self, event, where):
        found = self.matches(where)
        return found[-1] if found else None


@pytest.fixture(scope="module")
def plain():
    return sl.build_storyline(9, 30, n_sims=SIMS)


@pytest.fixture(scope="module")
def rich():
    return sl.build_storyline(9, 30, FakeHistory(), n_sims=SIMS)


def chance(doc, club, kind):
    return doc["facts"][f"{sl.slug(club)}_relegation_chance_{kind}pct"]


def test_document_follows_the_contract(plain):
    assert plain["id"] == "storyline-demo-0007" and plain["type"] == "storyline" and plain["match_id"] == "demo-0007"
    f = plain["facts"]
    assert (f["rovers_rank"], f["united_rank"], f["rovers_points"], f["united_points"]) == (17, 18, 29, 28)
    assert plain["focus"] == {"Rovers": "relegation", "United": "relegation"}
    assert set(f) == set(plain["field_meanings"]), "every fact has a meaning and nothing else"
    assert [len(x) for x in plain["form"].values()] == [5, 5]
    assert f["rovers_form"] == "".join(x["result"] for x in plain["form"]["Rovers"])


def test_deterministic():
    a = sl.build_storyline(9, 30, FakeHistory(), n_sims=SIMS, league=League.create(9, demo_matchday=30))
    b = sl.build_storyline(9, 30, FakeHistory(), n_sims=SIMS, league=League.create(9, demo_matchday=30))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_stakes_bounded_and_ordered(plain):
    f = plain["facts"]
    for club in ("Rovers", "United"):
        s = sl.slug(club)
        for e in sl.EVENTS:
            values = [f[f"{s}_{e}_chance{k}_pct"] for k in ("", "_if_win", "_if_draw", "_if_loss")]
            assert all(0 <= v <= 100 for v in values)
        win, draw, loss, now = (chance(plain, club, k) for k in ("if_win_", "if_draw_", "if_loss_", ""))
        assert win <= draw <= loss and win <= now <= loss, "staying up: if win >= now >= if loss"
        assert f[f"{s}_relegation_stake_pts"] == loss - win
    assert f["rovers_points_if_win"] == f["rovers_points"] + 3


def test_chances_match_match_importance():
    league = sl.league_for(9, 30)
    mi = match_importance(league, "Rovers", "United", 29, SIMS)
    win = sl.chances(league, "Rovers", "United", 29, (1, 0), SIMS)
    loss = sl.chances(league, "Rovers", "United", 29, (0, 1), SIMS)
    for e in sl.EVENTS:
        assert win["Rovers"][e] == mi["Rovers"][f"{e}_if_win"] and loss["United"][e] == mi["United"][f"{e}_if_win"]
        assert loss["Rovers"][e] == mi["Rovers"][f"{e}_if_loss"]


def test_threads_reference_only_storyline_facts(plain, rich):
    for doc in (plain, rich):
        assert 3 <= len(doc["threads"]) <= sl.MAX_THREADS
        assert [t["value"] for t in doc["threads"]] == sorted((t["value"] for t in doc["threads"]), reverse=True)
        for t in doc["threads"]:
            assert t["facts"], t["key"]
            for k, v in t["facts"].items():
                assert doc["facts"][k] == v and t["field_meanings"][k] == doc["field_meanings"][k]
            assert not any(ch.isdigit() for ch in t["angle"]), "numbers only come from the facts"
    top = plain["threads"][0]
    assert top["key"] == "relegation_six_pointer"
    table = next(t for t in plain["threads"] if t["key"] == "table")
    assert "drop zone" in table["angle"], "a win for either club moves the drop line on seed 9"


def test_pre_match_card_is_verified(plain, rich):
    for doc in (plain, rich):
        assert doc["verification"]["verified"] and doc["writer"] == "template"
        assert [t["lang"] for t in doc["texts"]] == ["en", "fr", "es"]
        assert all(doc["facts"][c["source_field"]] == c["value"] for t in doc["texts"] for c in t["claims"])


def test_without_history(plain):
    assert plain["history_through"] is None and plain["head_to_head"] is None and plain["players_to_watch"] == []
    assert not any(k.startswith("h2h_") or "_player" in k for k in plain["facts"])
    assert plain["facts"]["reverse_fixture"].startswith("matchday ")


def test_with_fake_history(rich):
    f = rich["facts"]
    assert f["history_through"] == "2025/26"
    assert (f["h2h_meetings"], f["h2h_rovers_wins"], f["h2h_draws"], f["h2h_united_wins"]) == (6, 1, 3, 2)
    assert f["h2h_last_meeting"] == "2025/26: United 0-0 Rovers"
    assert f["h2h_biggest_rovers_win"] == "2023/24: Rovers 4-0 United"
    assert f["h2h_biggest_united_win"] == "2024/25: United 3-1 Rovers", "same margin as 2025/26 Rovers 1-2 United: the widest, then the most recent"
    assert (f["rovers_titles"], f["united_best_finish"]) == (2, 3)
    watch = {p["name"]: p for p in rich["players_to_watch"]}
    assert set(watch) == {"Aldo Brenner", "Corin Vale"}, "Edgar Moss scored for Rovers but has left"
    assert watch["Aldo Brenner"]["top_scorer"] and watch["Aldo Brenner"]["season_goals"] == 3
    assert watch["Aldo Brenner"]["milestone"] == {"kind": "goals", "current": 98, "target": 100, "needed": 2}
    assert watch["Corin Vale"]["milestone"]["kind"] == "appearances" and watch["Corin Vale"]["milestone"]["target"] == 300
    keys = {t["key"] for t in rich["threads"]}
    assert "rovers_player1_milestone" in keys or "united_player1_milestone" in keys
    assert f["rovers_player1_name"] == "Aldo Brenner" and f["rovers_player1_milestone"] == "100 goals"


def live(doc, h, a, final=False):
    return sl.live_context(doc, {"score": {"home": h, "away": a}, "final": final})


def test_live_context_moves_with_the_score(plain):
    level, ahead, behind = live(plain, 0, 0)[0], live(plain, 1, 0)[0], live(plain, 0, 1)[0]
    for k in ("rovers_relegation_chance_if_score_holds_pct",):
        assert ahead[k] <= level[k] <= behind[k]
    k = "united_relegation_chance_if_score_holds_pct"
    assert behind[k] <= level[k] <= ahead[k]
    assert ahead["rovers_rank_if_score_holds"] <= level["rovers_rank_if_score_holds"] <= behind["rovers_rank_if_score_holds"]
    assert live(plain, 2, 0)[0]["rovers_relegation_chance_if_score_holds_pct"] <= ahead["rovers_relegation_chance_if_score_holds_pct"]
    assert level["rovers_relegation_chance_if_rovers_score_next_pct"] == ahead["rovers_relegation_chance_if_score_holds_pct"]
    assert level["united_relegation_chance_if_united_score_next_pct"] == behind["united_relegation_chance_if_score_holds_pct"]
    # seed 9: a United win swaps the two clubs across the drop line
    assert behind["united_leaves_drop_zone_if_score_holds"] and behind["rovers_enters_drop_zone_if_score_holds"]
    assert behind["united_relegation_change_direction"] == "down" and behind["rovers_relegation_change_direction"] == "up"
    assert ahead["rovers_relegation_chance_if_score_holds_pct"] == plain["facts"]["rovers_relegation_chance_if_win_pct"]


def test_live_context_fields_and_cache(plain):
    league = sl.league_for(9, 30)
    facts, meanings = live(plain, 3, 2)
    assert set(facts) == set(meanings) and facts["score_now"] == "Rovers 3-2 United"
    cached = len(sl._CHANCES[league])
    assert live(plain, 3, 2) == (facts, meanings) and len(sl._CHANCES[league]) == cached, "a score is simulated once"
    final = live(plain, 3, 2, final=True)[0]
    assert not any("score_next" in k for k in final)


def test_post_match(plain):
    facts, meanings = sl.post_match(plain, 5, 2)
    assert set(facts) == set(meanings) and facts["final_score"] == "Rovers 5-2 United"
    assert facts["rovers_rank_before"] == 17 and facts["rovers_points_after"] == 32 and facts["united_points_after"] == 28
    assert facts["rovers_relegation_chance_after_pct"] < facts["rovers_relegation_chance_before_pct"]
    assert facts["rovers_relegation_change_direction"] == "down" and facts["united_relegation_change_direction"] == "up"
    assert facts["rovers_relegation_chance_after_pct"] == live(plain, 5, 2, final=True)[0]["rovers_relegation_chance_if_score_holds_pct"]
    assert 1 <= facts["rovers_rank_after_matchday"] <= 20


def test_cli(tmp_path):
    out = tmp_path / "demo-0007" / "storyline.json"
    sl.main(["--league-seed", "9", "--matchday", "30", "--sims", str(SIMS), "--out", str(out)])
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["type"] == "storyline" and doc["league"] == {"seed": 9, "matchday": 30, "n_sims": SIMS}


# --- in the pipeline and the recap ------------------------------------------------------------------

@pytest.fixture(scope="module")
def desk_run(tmp_path_factory):
    from generator import generate_match
    from worker.desk import Desk
    from worker.pipeline import JsonFileSink, Pipeline
    from worker.writer import TemplateWriter

    root = tmp_path_factory.mktemp("desk")
    sink = JsonFileSink(root)
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True),
                        storyline_for=lambda match_id, teams: sl.build_storyline(9, 30, None, match_id, home=teams["home"], away=teams["away"]))
    feed, _ = generate_match(7, "demo")
    for m in feed:
        pipeline.handle(m)
    pipeline.flush()
    return root / feed[0]["match_id"]


def test_pipeline_writes_the_storyline_before_kick_off(desk_run):
    doc = json.loads((desk_run / "storyline.json").read_text(encoding="utf-8"))
    assert doc["type"] == "storyline" and doc["status"] == "approved" and doc["verification"]["verified"]
    fr = [l["line"] for l in doc["duo"] if l["lang"] == "fr"]
    assert any("17e" in l and "18e" in l for l in fr)  # the duo opens on the table, numbers claimed
    insights = desk_run / "insights.json"
    assert not insights.exists() or all(d["type"] != "storyline" for d in json.loads(insights.read_text(encoding="utf-8")))


def test_recap_says_what_the_result_changed_in_the_table(desk_run):
    from worker.recap import build_recap, load_desk

    cards, state = load_desk(desk_run)
    recap = build_recap(cards, state, storyline=json.loads((desk_run / "storyline.json").read_text(encoding="utf-8")))
    assert recap["verification"]["verified"]
    texts = {t["lang"]: t["recap"] for t in recap["texts"]}
    assert "Rovers move from 17th to" in texts["en"] and "risque de descente" in texts["fr"]  # the winner's season, rank and chance
    assert build_recap(cards, state)["facts"].keys().isdisjoint({k for k in recap["facts"] if k.startswith("season_")})


def test_the_worker_writes_the_recap_the_cli_would_write(desk_run, tmp_path, monkeypatch, capsys):
    """At the final whistle the worker writes recap.json in the match folder: the document `python -m worker.recap` writes from that folder, season sentence included."""
    import sys

    from worker import recap as recap_cli

    written = (desk_run / "recap.json").read_text(encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["recap", str(desk_run), "--out", str(tmp_path / "again.json")])
    recap_cli.main()
    capsys.readouterr()
    assert (tmp_path / "again.json").read_text(encoding="utf-8") == written
    doc = json.loads(written)
    assert (doc["id"], doc["type"], doc["final"], doc["writer"]) == ("recap-demo-0007", "recap", True, "template") and doc["verification"]["verified"]
    assert any(k.startswith("season_") for k in doc["facts"])


def test_goal_cards_tell_the_table_only_when_it_changes(desk_run):
    cards = sorted(json.loads((desk_run / "cards.json").read_text(encoding="utf-8")), key=lambda c: c["clock_s"])
    said = {c["minute_label"]: [l["line"] for l in c.get("duo", []) if l["lang"] == "en" and l["line"].startswith("If it stays")] for c in cards if c["moment"] == "goal"}
    assert said["14'"] == ["If it stays 0-1, Rovers drop into the drop zone, down to 18th."]
    assert said["31'"] == [] and said["52'"] == []  # still in the drop zone: nothing new
    assert said["57'"] and "climb out of the drop zone" in said["57'"][0]  # against the score before the goal, not kick-off
    assert said["66'"] and "14th" in said["66'"][0]  # the 63' goal was ruled out, so 66' says it again
    assert said["83'"] == []
    assert all(c["verification"]["verified"] for c in cards)
