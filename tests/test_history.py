import time
from collections import Counter

import numpy as np
import pytest

from generator import generate_match
from league.clubs import CLUBS
from league.history import History, demo_squads, frequencies, generate, link_demo
from league.history_matches import substitution_rules

SEED, SEASONS = 9, 5


@pytest.fixture(scope="module")
def h() -> History:
    return generate(SEED, SEASONS)


@pytest.fixture(scope="module")
def demo_info() -> dict:
    msgs, _ = generate_match(7, "demo", frames=False)
    return next(m for m in msgs if m["type"] == "match_info")


def _key(period: int, clock_s: int) -> tuple[int, int]:
    """Timeline order of a moment."""
    return period, clock_s


def test_deterministic(h):
    again = generate(SEED, SEASONS)
    assert again.meta == h.meta
    assert h.a.keys() == again.a.keys()
    for k in h.a:
        assert np.array_equal(h.a[k], again.a[k]), k
    other = generate(SEED + 1, SEASONS)
    assert not np.array_equal(other.a["m_hg"], h.a["m_hg"])


def test_save_and_load(h, tmp_path):
    folder = h.save(tmp_path)
    assert folder == tmp_path / str(SEED)
    loaded = History.load(folder)
    assert loaded.seasons() == h.seasons()
    assert loaded.count("goal") == h.count("goal")
    pid = h.demo_ids["home-20"]
    assert loaded.player(pid) == h.player(pid)
    assert loaded.head_to_head("Varleigh", "Yarnford") == h.head_to_head("Varleigh", "Yarnford")


def test_api_shapes(h, demo_info):
    assert h.seasons() == ["2021/22", "2022/23", "2023/24", "2024/25", "2025/26"]
    club = h.club("Varleigh")
    assert set(club) == {"name", "founded_season", "seasons_in_league", "titles", "best_finish"}
    assert club["founded_season"][4] == "/" and 0 <= club["seasons_in_league"] <= SEASONS
    with pytest.raises(KeyError):
        h.club("Nowhere Town")
    m = h.matches(lambda m: m["home"] == "Varleigh")[0]
    assert {"id", "season", "matchday", "home", "away", "home_goals", "away_goals", "half_time", "goals", "substitutions", "cards", "lineups"} <= set(m)
    p = h.player(h.demo_ids["home-20"])
    assert set(p) == {"id", "name", "position", "born", "clubs", "goals", "appearances", "sub_appearances"}
    sub = next(q["name"] for q in demo_info["teams"]["home"]["bench"] if q["id"] == "home-20")
    assert p["name"] == sub and p["position"] == "ST" and p["clubs"][-1] == {"season": "2025/26", "club": "Varleigh"}
    assert 0 <= p["sub_appearances"] <= p["appearances"]
    with pytest.raises(KeyError):
        h.player("P999999")
    with pytest.raises(ValueError):
        h.count("goals")
    for event in ("match", "goal", "substitution", "card"):
        e = h.last(event)
        assert e is not None and e["season"] == "2025/26"
        if event != "match":
            assert e["match"]["id"] == e["match_id"] and {"minute", "stoppage", "minute_label", "team", "club"} <= set(e)


def test_head_to_head_newest_first(h):
    rows = h.head_to_head("Varleigh", "Yarnford")
    assert rows and rows == h.head_to_head("Yarnford", "Varleigh")
    assert {"season", "matchday", "home", "away", "home_goals", "away_goals", "half_time"} == set(rows[0])
    order = [(h.seasons().index(r["season"]), r["matchday"]) for r in rows]
    assert order == sorted(order, reverse=True)
    assert all({r["home"], r["away"]} == {"Varleigh", "Yarnford"} for r in rows)
    n = h.count("match", lambda m: {m["home"], m["away"]} == {"Varleigh", "Yarnford"})
    assert len(rows) == n


def test_promotion_and_relegation(h):
    for season in h.seasons():
        assert len(h.clubs(season)) == 20
        assert len(h.matches(lambda m: m["season"] == season)) == 380
        table = h.table(season)
        assert [r["rank"] for r in table] == list(range(1, 21)) and all(r["played"] == 38 for r in table)
    for before, after in zip(h.seasons(), h.seasons()[1:]):
        down = set(h.clubs(before)) - set(h.clubs(after))
        up = set(h.clubs(after)) - set(h.clubs(before))
        assert down == {r["club"] for r in h.table(before) if r["rank"] >= 18}
        assert up == {r["club"] for r in h.table(before, tier=2) if r["rank"] <= 3}
    last = h.seasons()[-1]
    next_season = {r["club"] for r in h.table(last) if r["rank"] <= 17} | {r["club"] for r in h.table(last, tier=2) if r["rank"] <= 3}
    assert next_season == set(CLUBS), "the season after the history is the one of League.create(seed)"
    assert {"Varleigh", "Yarnford"} <= set(h.clubs(last))


def test_tables_match_the_scores(h):
    season = h.seasons()[2]
    points = Counter()
    for m in h.matches(lambda m: m["season"] == season):
        hg, ag = m["home_goals"], m["away_goals"]
        points[m["home"]] += 3 if hg > ag else hg == ag
        points[m["away"]] += 3 if ag > hg else hg == ag
    assert points == {r["club"]: r["points"] for r in h.table(season)}


def test_careers_bounded(h):
    a = h.a
    years = np.asarray(h.years)[a["ps_season"]]
    age = years - a["p_born"][a["ps_player"]]
    assert age.min() >= 16 and age.max() <= 37
    pairs = a["ps_player"].astype(np.int64) * 100 + a["ps_season"]
    assert np.unique(pairs).size == pairs.size, "one club per player and season"
    for s in range(len(h.years)):
        players = a["ps_player"][a["ps_season"] == s]
        names = [h.names[p] for p in players]
        assert len(set(names)) == len(names), "names repeat across eras, never within a season"
        sizes = np.bincount(a["ps_club"][a["ps_season"] == s])
        assert sizes.min() >= 22 and sizes.max() <= 28
    assert len(h.names) < 100_000 and h.ids[0] == "P000001"


def test_every_goal_has_a_scorer_on_the_pitch(h):
    for m in h.matches(lambda m: m["season"] in ("2021/22", "2025/26")):
        apps = {(x["player"], x["team"]): x for x in h.appearances(m["id"])}
        side_goals = Counter(g["team"] for g in m["goals"])
        assert (side_goals["home"], side_goals["away"]) == (m["home_goals"], m["away_goals"])
        assert sum(g["period"] == 1 and g["team"] == "home" for g in m["goals"]) == m["half_time"]["home"]
        assert sum(g["period"] == 1 and g["team"] == "away" for g in m["goals"]) == m["half_time"]["away"]
        for g in m["goals"]:
            team = g["team"] if not g["own_goal"] else ("away" if g["team"] == "home" else "home")
            x = apps[(g["player"], team)]
            t = _key(g["period"], g["clock_s"])
            assert x["on"] is None or _key(x["on"]["period"], x["on"]["clock_s"]) <= t
            assert x["off"] is None or t < _key(x["off"]["period"], x["off"]["clock_s"])
            assert g["by_substitute"] == (not x["started"])
            if g["assist"]:
                y = apps[(g["assist"], g["team"])]
                assert g["assist"] != g["player"] and (y["on"] is None or _key(y["on"]["period"], y["on"]["clock_s"]) <= t)
        max_subs, bench, _ = substitution_rules(int(m["season"][:4]))
        for side in ("home", "away"):
            subs = [s for s in m["substitutions"] if s["team"] == side]
            assert len(subs) <= max_subs and len(m["lineups"][side]["starting"]) == 11 and len(m["lineups"][side]["bench"]) <= bench
            for s in subs:
                assert s["player_on"] in m["lineups"][side]["bench"] and s["player_off"] in m["lineups"][side]["starting"] + m["lineups"][side]["bench"]
        for c in m["cards"]:
            x = apps[(c["player"], c["team"])]
            t = _key(c["period"], c["clock_s"])
            assert (x["on"] is None or _key(x["on"]["period"], x["on"]["clock_s"]) <= t) and (x["off"] is None or t <= _key(x["off"]["period"], x["off"]["clock_s"]))


def test_count_and_last_agree(h):
    predicates = {
        "goal": [lambda g: g["by_substitute"], lambda g: g["penalty"] and g["minute"] >= 90, lambda g: g["name"] == "Nobody At All"],
        "match": [lambda m: m["home_goals"] >= 5, lambda m: m["half_time"]["home"] > m["half_time"]["away"] and m["home_goals"] < m["away_goals"]],
        "card": [lambda c: c["card"] == "second_yellow"],
        "substitution": [lambda s: s["minute"] == 46],
    }
    for event, preds in predicates.items():
        for where in preds:
            n, last = h.count(event, where), h.last(event, where)
            assert (last is None) == (n == 0)
            if last is not None:
                assert where(last)
                matching = [m for m in h.matches() if (where(m) if event == "match" else any(where(e) for e in m[{"goal": "goals", "card": "cards", "substitution": "substitutions"}[event]]))]
                assert (last if event == "match" else last["match"]) is matching[-1]
    assert h.count("match") == 380 * SEASONS


def test_link_demo(h, demo_info):
    squads = demo_squads()
    assert [p.name for p in squads["Varleigh"]] == [p["name"] for p in demo_info["teams"]["home"]["starting"] + demo_info["teams"]["home"]["bench"]]
    link = link_demo(h, demo_info)
    assert len(link) == 40 and all(link.values())
    assert link == h.demo_ids
    last = h.seasons()[-1]
    for key, pid in link.items():
        club = "Varleigh" if key.startswith("home") else "Yarnford"
        p = h.player(pid)
        assert p["clubs"][-1] == {"season": last, "club": club}
        assert h.find_players(p["name"], last) == [pid]


def test_frequencies_short(h):
    f = frequencies(h)
    assert 5 <= f["hat_tricks_per_season"] <= 25  # Premier League: about 8 to 15 a season
    assert 2.6 <= f["goals_per_season"] / 380 <= 3.1
    assert 20 <= f["red_cards_per_season"] <= 80 and 15 <= f["own_goals_per_season"] <= 50


MAX_FIFTY_SEASONS_S = 120


def test_fifty_seasons():
    start = time.perf_counter()
    h50 = generate(SEED, 50)
    f = frequencies(h50)
    elapsed = time.perf_counter() - start
    # A ceiling against a runaway slowdown, not a benchmark: wide enough that a slow or busy machine does not fail the suite
    assert elapsed < MAX_FIFTY_SEASONS_S, f"50 seasons took {elapsed:.0f} s (ceiling {MAX_FIFTY_SEASONS_S} s)"
    assert h50.seasons()[0] == "1976/77" and h50.count("match") == 19_000
    assert 8 <= f["hat_tricks_per_season"] <= 18
    assert 0.5 <= f["four_or_more_per_season"] <= 4
    assert 1 <= f["sub_braces_per_season"] <= 6 and f["sub_hat_tricks_total"] <= 20
    assert 1 <= f["comebacks_from_two_down_per_season"] <= 6 and 5 <= f["late_winners_per_season"] <= 25
    assert 30 <= f["red_cards_per_season"] <= 70
    varleigh, yarnford = h50.club("Varleigh"), h50.club("Yarnford")
    assert varleigh["seasons_in_league"] >= 20 and yarnford["seasons_in_league"] >= 20 and varleigh["titles"] + yarnford["titles"] >= 1
    # careers: a debut around 18, the last season by 37
    first = {}
    for p, s in zip(h50.a["ps_player"].tolist(), h50.a["ps_season"].tolist()):
        first.setdefault(p, s)
    debut_age = [h50.years[s] - int(h50.a["p_born"][p]) for p, s in first.items() if s > 0]
    assert 17 <= np.median(debut_age) <= 19
