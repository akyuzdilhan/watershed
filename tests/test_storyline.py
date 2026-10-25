import json

import pytest

from league.season import League, match_importance
from worker import storyline as sl

SIMS = 1000  # enough for ordered chances on seed 9, fast enough for the suite


class FakeHistory:
    """The read API of `league.history.History` (docs/contracts.md, Lot 2), on a handful of matches."""

    SEASONS = ["2023/24", "2024/25", "2025/26"]
    CLUBS = {"Varleigh": {"name": "Varleigh", "founded_season": "1976/77", "seasons_in_league": 50, "titles": 2, "best_finish": 1},
             "Yarnford": {"name": "Yarnford", "founded_season": "1976/77", "seasons_in_league": 41, "titles": 0, "best_finish": 3}}
    PLAYERS = {
        "P000001": {"id": "P000001", "name": "Aldo Brenner", "position": "FW", "born": 1998, "clubs": [{"season": "2025/26", "club": "Varleigh"}],
                    "goals": 98, "appearances": 210, "sub_appearances": 12},
        "P000002": {"id": "P000002", "name": "Corin Vale", "position": "MF", "born": 1994, "clubs": [{"season": "2025/26", "club": "Yarnford"}],
                    "goals": 31, "appearances": 299, "sub_appearances": 40},
        "P000003": {"id": "P000003", "name": "Edgar Moss", "position": "FW", "born": 1999, "clubs": [{"season": "2025/26", "club": "Dunholt"}],
                    "goals": 49, "appearances": 120, "sub_appearances": 5},
    }

    def __init__(self):
        def m(season, md, home, away, hg, ag, scorers=()):
            return {"season": season, "matchday": md, "home": home, "away": away, "home_goals": hg, "away_goals": ag, "half_time": [0, 0],
                    "scorers": [{"player_id": p, "club": c, "minute": 10 + k} for k, (p, c) in enumerate(scorers)]}
        self._matches = [
            m("2023/24", 5, "Varleigh", "Yarnford", 4, 0), m("2023/24", 24, "Yarnford", "Varleigh", 1, 1),
            m("2024/25", 8, "Yarnford", "Varleigh", 3, 1), m("2024/25", 27, "Varleigh", "Yarnford", 2, 2),
            m("2025/26", 3, "Varleigh", "Dunholt", 3, 1, [("P000001", "Varleigh"), ("P000001", "Varleigh"), ("P000003", "Varleigh"), ("P000009", "Dunholt")]),
            m("2025/26", 9, "Varleigh", "Yarnford", 1, 2, [("P000001", "Varleigh"), ("P000002", "Yarnford"), ("P000002", "Yarnford")]),
            m("2025/26", 28, "Yarnford", "Varleigh", 0, 0),
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
    assert (f["varleigh_rank"], f["yarnford_rank"], f["varleigh_points"], f["yarnford_points"]) == (17, 18, 29, 28)
    assert plain["focus"] == {"Varleigh": "relegation", "Yarnford": "relegation"}
    assert set(f) == set(plain["field_meanings"]), "every fact has a meaning and nothing else"
    assert [len(x) for x in plain["form"].values()] == [5, 5]
    assert f["varleigh_form"] == "".join(x["result"] for x in plain["form"]["Varleigh"])


def test_deterministic():
    a = sl.build_storyline(9, 30, FakeHistory(), n_sims=SIMS, league=League.create(9, demo_matchday=30))
    b = sl.build_storyline(9, 30, FakeHistory(), n_sims=SIMS, league=League.create(9, demo_matchday=30))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_stakes_bounded_and_ordered(plain):
    f = plain["facts"]
    for club in ("Varleigh", "Yarnford"):
        s = sl.slug(club)
        for e in sl.EVENTS:
            values = [f[f"{s}_{e}_chance{k}_pct"] for k in ("", "_if_win", "_if_draw", "_if_loss")]
            assert all(0 <= v <= 100 for v in values)
        win, draw, loss, now = (chance(plain, club, k) for k in ("if_win_", "if_draw_", "if_loss_", ""))
        assert win <= draw <= loss and win <= now <= loss, "staying up: if win >= now >= if loss"
        assert f[f"{s}_relegation_stake_pts"] == loss - win
    assert f["varleigh_points_if_win"] == f["varleigh_points"] + 3


def test_chances_match_match_importance():
    league = sl.league_for(9, 30)
    mi = match_importance(league, "Varleigh", "Yarnford", 29, SIMS)
    win = sl.chances(league, "Varleigh", "Yarnford", 29, (1, 0), SIMS)
    loss = sl.chances(league, "Varleigh", "Yarnford", 29, (0, 1), SIMS)
    for e in sl.EVENTS:
        assert win["Varleigh"][e] == mi["Varleigh"][f"{e}_if_win"] and loss["Yarnford"][e] == mi["Yarnford"][f"{e}_if_win"]
        assert loss["Varleigh"][e] == mi["Varleigh"][f"{e}_if_loss"]


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
    assert (f["h2h_meetings"], f["h2h_varleigh_wins"], f["h2h_draws"], f["h2h_yarnford_wins"]) == (6, 1, 3, 2)
    assert f["h2h_last_meeting"] == "2025/26: Yarnford 0-0 Varleigh"
    assert f["h2h_biggest_varleigh_win"] == "2023/24: Varleigh 4-0 Yarnford"
    assert f["h2h_biggest_yarnford_win"] == "2024/25: Yarnford 3-1 Varleigh", "same margin as 2025/26 Varleigh 1-2 Yarnford: the widest, then the most recent"
    assert (f["varleigh_titles"], f["yarnford_best_finish"]) == (2, 3)
    watch = {p["name"]: p for p in rich["players_to_watch"]}
    assert set(watch) == {"Aldo Brenner", "Corin Vale"}, "Edgar Moss scored for Varleigh but has left"
    assert watch["Aldo Brenner"]["top_scorer"] and watch["Aldo Brenner"]["season_goals"] == 3
    assert watch["Aldo Brenner"]["milestone"] == {"kind": "goals", "current": 98, "target": 100, "needed": 2}
    assert watch["Corin Vale"]["milestone"]["kind"] == "appearances" and watch["Corin Vale"]["milestone"]["target"] == 300
    keys = {t["key"] for t in rich["threads"]}
    assert "varleigh_player1_milestone" in keys or "yarnford_player1_milestone" in keys
    assert f["varleigh_player1_name"] == "Aldo Brenner" and f["varleigh_player1_milestone"] == "100 goals"


def live(doc, h, a, final=False):
    return sl.live_context(doc, {"score": {"home": h, "away": a}, "final": final})


def test_live_context_moves_with_the_score(plain):
    level, ahead, behind = live(plain, 0, 0)[0], live(plain, 1, 0)[0], live(plain, 0, 1)[0]
    for k in ("varleigh_relegation_chance_if_score_holds_pct",):
        assert ahead[k] <= level[k] <= behind[k]
    k = "yarnford_relegation_chance_if_score_holds_pct"
    assert behind[k] <= level[k] <= ahead[k]
    assert ahead["varleigh_rank_if_score_holds"] <= level["varleigh_rank_if_score_holds"] <= behind["varleigh_rank_if_score_holds"]
    assert live(plain, 2, 0)[0]["varleigh_relegation_chance_if_score_holds_pct"] <= ahead["varleigh_relegation_chance_if_score_holds_pct"]
    assert level["varleigh_relegation_chance_if_varleigh_score_next_pct"] == ahead["varleigh_relegation_chance_if_score_holds_pct"]
    assert level["yarnford_relegation_chance_if_yarnford_score_next_pct"] == behind["yarnford_relegation_chance_if_score_holds_pct"]
    # seed 9: a Yarnford win swaps the two clubs across the drop line
    assert behind["yarnford_leaves_drop_zone_if_score_holds"] and behind["varleigh_enters_drop_zone_if_score_holds"]
    assert behind["yarnford_relegation_change_direction"] == "down" and behind["varleigh_relegation_change_direction"] == "up"
    assert ahead["varleigh_relegation_chance_if_score_holds_pct"] == plain["facts"]["varleigh_relegation_chance_if_win_pct"]


def test_live_context_fields_and_cache(plain):
    league = sl.league_for(9, 30)
    facts, meanings = live(plain, 3, 2)
    assert set(facts) == set(meanings) and facts["score_now"] == "Varleigh 3-2 Yarnford"
    cached = len(sl._CHANCES[league])
    assert live(plain, 3, 2) == (facts, meanings) and len(sl._CHANCES[league]) == cached, "a score is simulated once"
    final = live(plain, 3, 2, final=True)[0]
    assert not any("score_next" in k for k in final)


def test_post_match(plain):
    facts, meanings = sl.post_match(plain, 5, 2)
    assert set(facts) == set(meanings) and facts["final_score"] == "Varleigh 5-2 Yarnford"
    assert facts["varleigh_rank_before"] == 17 and facts["varleigh_points_after"] == 32 and facts["yarnford_points_after"] == 28
    assert facts["varleigh_relegation_chance_after_pct"] < facts["varleigh_relegation_chance_before_pct"]
    assert facts["varleigh_relegation_change_direction"] == "down" and facts["yarnford_relegation_change_direction"] == "up"
    assert facts["varleigh_relegation_chance_after_pct"] == live(plain, 5, 2, final=True)[0]["varleigh_relegation_chance_if_score_holds_pct"]
    assert 1 <= facts["varleigh_rank_after_matchday"] <= 20


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
    assert "Varleigh move from 17th to" in texts["en"] and "risque de descente" in texts["fr"]  # the winner's season, rank and chance
    assert build_recap(cards, state)["facts"].keys().isdisjoint({k for k in recap["facts"] if k.startswith("season_")})


def test_goal_cards_tell_the_table_only_when_it_changes(desk_run):
    cards = sorted(json.loads((desk_run / "cards.json").read_text(encoding="utf-8")), key=lambda c: c["clock_s"])
    said = {c["minute_label"]: [l["line"] for l in c.get("duo", []) if l["lang"] == "en" and l["line"].startswith("If it stays")] for c in cards if c["moment"] == "goal"}
    assert said["14'"] == ["If it stays 0-1, Varleigh drop into the drop zone, down to 18th."]
    assert said["31'"] == [] and said["52'"] == []  # still in the drop zone: nothing new
    assert said["57'"] and "climb out of the drop zone" in said["57'"][0]  # against the score before the goal, not kick-off
    assert said["66'"] and "14th" in said["66'"][0]  # the 63' goal was ruled out, so 66' says it again
    assert said["83'"] == []
    assert all(c["verification"]["verified"] for c in cards)
