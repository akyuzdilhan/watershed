import json
import math
import random

import pytest

from generator import generate_match
from worker.insight_history import HistoryView, comeback_stage
from worker.insight_rarity import bits, laplace, poisson_sf, value
from worker.pipeline import JsonFileSink, MemorySink, Pipeline, ThreadedSink

CLUBS = ["Rovers", "United"] + [f"Club {c}" for c in "ABCDEFGHIJKLMNOPQR"]


class FakeHistory:
    """A small seeded league history with the read API of the contract (league.history.History)."""

    def __init__(self, seasons: int = 12, seed: int = 3) -> None:
        rng = random.Random(seed)
        self._seasons = [f"{1976 + i}/{(77 + i) % 100:02d}" for i in range(seasons)]
        self._matches = []
        self._players = {}
        squads = {}
        n = 0
        for club in CLUBS:
            squads[club] = []
            for _ in range(18):
                n += 1
                pid = f"P{n:06d}"
                self._players[pid] = {"id": pid, "name": f"Player {n}", "position": "FW", "born": 1950, "clubs": [], "goals": 0, "appearances": 0, "sub_appearances": 0}
                squads[club].append(pid)
        for season in self._seasons:
            fixtures = [(h, a) for h in CLUBS for a in CLUBS if h != a]
            rng.shuffle(fixtures)
            for i, (home, away) in enumerate(fixtures):
                self._matches.append(self._play(rng, season, i // 10 + 1, home, away, squads))

    @staticmethod
    def _poisson(rng, mu):
        k, p, l = 0, 1.0, math.exp(-mu)
        while True:
            p *= rng.random()
            if p < l:
                return k
            k += 1

    def _play(self, rng, season, matchday, home, away, squads):
        subs, goals, cards = [], [], []
        bench = {}
        for side, club in (("home", home), ("away", away)):
            bench[side] = []
            for j in range(3):
                pin = squads[club][11 + j]
                minute = rng.randint(46, 85)
                subs.append({"team": side, "player_in": pin, "player_out": squads[club][8 + j], "minute": minute})
                bench[side].append((pin, minute))
            if rng.random() < 0.08:
                cards.append({"team": side, "player_id": squads[club][rng.randint(1, 10)], "minute": rng.randint(20, 89), "card": "red"})
        for side, club, mu in (("home", home, 1.5), ("away", away, 1.2)):
            for _ in range(self._poisson(rng, mu)):
                minute = rng.randint(1, 90)
                on = [p for p, mn in bench[side] if mn < minute]
                scorer = rng.choice(on) if on and rng.random() < 0.15 else squads[club][rng.randint(1, 10)]
                goals.append({"team": side, "player_id": scorer, "minute": minute})
        goals.sort(key=lambda g: g["minute"])
        hg = sum(1 for g in goals if g["team"] == "home")
        ag = len(goals) - hg
        ht = {"home": sum(1 for g in goals if g["team"] == "home" and g["minute"] <= 45), "away": sum(1 for g in goals if g["team"] == "away" and g["minute"] <= 45)}
        return {"season": season, "matchday": matchday, "home": home, "away": away, "home_goals": hg, "away_goals": ag, "half_time": ht,
                "goals": goals, "substitutions": subs, "cards": cards}

    def seasons(self):
        return list(self._seasons)

    def club(self, name):
        return {"name": name, "founded_season": self._seasons[0], "seasons_in_league": len(self._seasons), "titles": 0, "best_finish": 1}

    def head_to_head(self, home, away):
        return [self._row(m) for m in reversed(self._matches) if m["home"] == home and m["away"] == away]

    @staticmethod
    def _row(m):
        return {k: m[k] for k in ("season", "matchday", "home", "away", "home_goals", "away_goals", "half_time")}

    def matches(self, where):
        return [m for m in self._matches if where(m)]

    def player(self, player_id):
        return self._players[player_id]

    def count(self, event, where):
        return sum(1 for m in self._matches if where(m))

    def last(self, event, where):
        for m in reversed(self._matches):
            if where(m):
                return {"season": m["season"], "match": m}
        return None


@pytest.fixture(scope="module")
def history():
    return FakeHistory()


@pytest.fixture(scope="module")
def demo():
    return generate_match(7, "demo")


def run(stream, history, sink=None, desk=None):
    pipeline = Pipeline(sink=sink or MemorySink(), desk=desk, history=history)
    for m in stream:
        pipeline.handle(m)
    pipeline.flush()
    return pipeline


@pytest.fixture(scope="module")
def demo_run(demo, history):
    pipeline = run(demo[0], history)
    return pipeline, {d["key"]: d for d in pipeline.insights("demo-0007")}


def voided_goals(truth):
    return {g["event_id"] for g in truth["goals"] if not g["counts"]}


def test_rarity_worked_example():
    # 0 substitutes with four goals in 18,620 matches: Laplace gives 1 / 18,622, about 14.2 bits, the top of the scale
    p = laplace(0, 18620)
    assert p == pytest.approx(1 / 18622)
    assert bits(p) == pytest.approx(14.18, abs=0.01)
    assert value(bits(p), 1.0) == 100
    assert value(1.0, 0.0) == 6  # a fact seen every other match is worth little
    assert poisson_sf(5, 2.0) == pytest.approx(0.0527, abs=1e-4)
    assert comeback_stage(["away", "away", "home", "home", "home"], "home", final=True) == (2, 3)
    assert comeback_stage(["away", "away", "home", "home"], "home", final=False) == (2, 1)


def test_history_view_reads_the_contract_api(history):
    view = HistoryView(history)
    assert view.n == 12 * 380 and view.first_season == "1976/77" and view.current_season == "1988/89"
    assert all(f.goals_of("home") == sum(1 for g in f.goals if g.side == "home") for f in view.features[:200])
    assert view.runs()["current"]["Rovers"]["unbeaten"] >= 0


def test_demo_substitute_scores_four_never_before(demo_run, demo):
    _, docs = demo_run
    four = docs["scorer-home-20-4"]
    hf = four["headline_facts"]
    assert four["status"] == "proposed" and four["kind"] == "milestone"
    assert (hf["goals"], hf["substitute"], hf["came_on"], hf["goal_minutes"]) == (4, True, "46'", "52', 57', 66', 83'")
    assert hf["history_count"] == 0 and "never before" in four["rarity"]["basis"]
    assert four["rarity"]["basis"].startswith("4 or more goals by a substitute: 0 in 4,560 matches since 1976/77")
    assert four["rarity"]["surprisal_bits"] > 12 and four["value"] >= 75
    assert set(four["evidence"]).isdisjoint(voided_goals(demo[1]))


def test_scorer_fact_is_superseded_brace_hat_trick_four(demo_run):
    _, docs = demo_run
    brace, hat_trick = docs["scorer-home-20-2"], docs["scorer-home-20-3"]
    assert brace["status"] == hat_trick["status"] == "superseded"
    assert hat_trick["superseded_by"] == "insight-demo-0007-scorer-home-20-4"
    # the 63' hat-trick is ruled out by VAR, then scored again at 66': superseded, back to the brace, revived
    actions = [h["action"] for h in hat_trick["history"]]
    assert actions[:3] == ["proposed", "superseded", "revived"] and hat_trick["version"] >= 2
    assert hat_trick["headline_facts"]["goal_minutes"] == "52', 57', 66'"
    active = [d for d in docs.values() if d["group"] == "scorer-home-20" and d["status"] != "superseded"]
    assert [d["key"] for d in active] == ["scorer-home-20-4"]


def test_demo_comeback_from_two_down(demo_run):
    _, docs = demo_run
    won = docs["comeback-home-2-3"]
    hf = won["headline_facts"]
    assert won["status"] == "proposed" and (hf["team"], hf["deficit_goals"], hf["trailing_score"], hf["stage"], hf["score"]) == ("Rovers", 2, "0-2", "won", "5-2")
    assert 0 < hf["history_count"] < hf["history_opportunities"]
    assert 0.005 < won["rarity"]["p"] < 0.3  # sensible: a comeback from two down is rare, not unheard of
    assert won["rarity"]["basis"].startswith("teams 2 or more goals down that won: ")
    for stage in ("2-1", "2-2"):
        assert docs[f"comeback-home-{stage}"]["status"] == "superseded"
    lost = docs["ht-lead-lost-away-2"]
    assert (lost["headline_facts"]["team"], lost["headline_facts"]["half_time_score"], lost["headline_facts"]["stage"]) == ("United", "0-2", "lost")


def test_voided_goals_are_never_counted(demo_run, demo):
    _, docs = demo_run
    void = voided_goals(demo[1])
    assert len(void) == 2
    for d in docs.values():
        if d["status"] != "superseded":
            assert set(d.get("evidence", [])).isdisjoint(void), d["key"]
    assert docs["scorer-home-20-3"]["supersede_cause"] == "updated"
    # the hat-trick that only counted the 63' goal was taken back by the void
    assert any(h["action"] == "superseded" for h in docs["comeback-home-2-2"]["history"])


def test_stat_of_the_match(demo_run):
    _, docs = demo_run
    stat = docs["stat-of-the-match"]
    hf = stat["headline_facts"]
    assert stat["stat_of_the_match"] and hf["top_fact"] == "scorer-home-20-4"
    assert hf["runner_up"] != hf["top_fact"] and hf["times_rarer_than_runner_up"] >= 1
    assert "ranks first" in stat["why_first"] and 2 <= len(stat["ranked"]) <= 5
    assert stat["ranked"][0]["key"] == "scorer-home-20-4"
    assert [r["value"] for r in stat["ranked"]] == sorted((r["value"] for r in stat["ranked"]), reverse=True)


def test_every_insight_follows_the_contract(demo_run):
    _, docs = demo_run
    for d in docs.values():
        assert d["id"] == f"insight-demo-0007-{d['key']}" and d["type"] == "insight"
        assert d["kind"] in ("record", "rarity", "streak", "milestone", "storyline", "tactical")
        assert d["status"] in ("proposed", "pending", "approved", "rejected", "superseded")
        assert 0 <= d["value"] <= 100 and 0 < d["rarity"]["p"] <= 1 and d["rarity"]["basis"]
        assert set(d["headline_facts"]) <= set(d["field_meanings"])
        assert "texts" not in d  # no desk


def test_cap_per_period_bounds_what_is_proposed(demo_run):
    pipeline, docs = demo_run
    engine = pipeline.engines["demo-0007"]
    assert all(n <= 4 for n in engine.admitted.values())
    assert len(docs) <= 20


def test_deterministic_and_order_independent(demo, history, demo_run):
    _, docs = demo_run
    again = {d["key"]: d for d in run(demo[0], history).insights("demo-0007")}
    assert json.dumps(again, sort_keys=True) == json.dumps(docs, sort_keys=True)
    shuffled = list(demo[0])
    rng = random.Random(1)
    for i in range(0, len(shuffled) - 4, 4):
        if rng.random() < 0.3:
            shuffled[i], shuffled[i + 2] = shuffled[i + 2], shuffled[i]
    shuffled += rng.sample(demo[0], 50)
    other = run(shuffled, history).insights("demo-0007")
    live = lambda ds: sorted(d["key"] for d in ds if d["status"] != "superseded")
    assert live(other) == live(docs.values())


def lost_leads_hold(pipeline) -> bool:
    """Every active "led at half-time, now trail" insight is true at the score as it stands."""
    for match_id, engine in pipeline.engines.items():
        score = pipeline.matches[match_id].score()
        for d in engine.active():
            if d["group"].startswith("ht-lead-lost-") and d["headline_facts"]["stage"] == "behind":
                side = d["group"].rsplit("-", 1)[1]
                if score[side] >= score["away" if side == "home" else "home"]:
                    return False
    return True


@pytest.mark.parametrize("seed", range(1, 21))
def test_unscripted_matches(seed, history):
    stream, _ = generate_match(seed, frames=False)
    pipeline = Pipeline(sink=MemorySink(), history=history)
    for m in stream:
        pipeline.handle(m)
        assert lost_leads_hold(pipeline), m["seq"]
    pipeline.flush()
    docs = pipeline.insights(f"synthetic-{seed:04d}")
    live = [d for d in docs if d["status"] != "superseded"]
    assert len(live) <= 3 * 4 + 1 and len(docs) <= 40
    assert sum(1 for d in docs if d.get("stat_of_the_match")) <= 1


# --- what holds at the current score, and while VAR checks a goal --------------------------------------

def test_a_lost_half_time_lead_is_told_only_while_the_team_trails(demo, history):
    """United lead at the break, trail from the second Rovers comeback goal, are level when their goal stands for a minute, trail again when VAR rules it out."""
    stream, truth = demo
    pipeline = Pipeline(sink=MemorySink(), history=history)
    for m in stream:
        pipeline.handle(m)
        assert lost_leads_hold(pipeline), m["seq"]
    pipeline.flush()
    doc = pipeline.engines["demo-0007"].docs["ht-lead-lost-away-1"]
    assert [h["action"] for h in doc["history"]] == ["proposed", "superseded", "revived", "superseded", "revived", "superseded"]
    causes = [e["cause"] for e in pipeline.sink.audit if e["card_id"] == doc["id"] and e["action"] == "superseded"]
    assert causes == ["goal ruled out", "no longer true", "updated"]  # the 63' goal voided, United's equaliser, then lost at full time
    equaliser = next(g["event_id"] for g in truth["goals"] if g["team"] == "away" and not g["counts"])
    assert equaliser not in doc["evidence"] and doc["superseded_by"] == "insight-demo-0007-ht-lead-lost-away-2"


def craft(template: dict, seq: int, emitted_at_s: float, minute: int, **changes) -> dict:
    """A message of the second half modelled on `template`."""
    return {**template, "message_id": f"extra-{seq}", "seq": seq, "emitted_at_s": emitted_at_s, "period": 2, "clock_s": 60.0 * minute + 10, "minute": minute,
            "minute_label": f"{minute + 1}'", **changes}


def test_facts_that_rest_on_the_score_are_under_review_while_a_goal_is_checked(demo, history):
    """The demo until United go down to ten, then two goals of our own: United score with ten men (3-3), Rovers go 4-3 and VAR checks that goal. The fact on United's goal with ten men shows the score: it is under review, though it does not count the Rovers goal; United's brace does not rest on it."""
    stream, truth = demo
    red = next(m for m in stream if m["type"] == "action" and m.get("card") in ("red", "second_yellow"))
    kept = [m for m in stream if m["seq"] <= red["seq"]]
    goal = {side: next(m for m in stream if m["type"] == "action" and m["event_id"] == next(g["event_id"] for g in truth["goals"] if g["team"] == side and g["counts"]))
            for side in ("home", "away")}
    started = next(m for m in stream if m["type"] == "var_check" and m["status"] == "started")
    t, s = max(m["emitted_at_s"] for m in kept), red["seq"]
    away = craft(goal["away"], s + 1, t + 60, 79, event_id="extra-away-goal")
    home = craft(goal["home"], s + 2, t + 120, 81, event_id="extra-home-goal")
    check = craft(started, s + 3, t + 135, 81, check_id="extra-var", target_event_id="extra-home-goal")
    stands = craft(check, s + 4, t + 180, 82, status="completed", decision="stands")
    pipeline = Pipeline(sink=MemorySink(), history=history, insight_min_value=0, insight_cap=100)
    for m in kept + [away, home]:
        pipeline.handle(m)
    engine = pipeline.engines["demo-0007"]
    ten_men, brace = engine.docs["ten-men-away-scored"], engine.docs[f"scorer-{goal['away']['player']['id']}-2"]
    assert engine.current["ten-men-away"] == "ten-men-away-scored" and ten_men["headline_facts"]["score"] == "4-3"
    assert "extra-home-goal" not in ten_men["evidence"] and not ten_men["under_review"]
    pipeline.handle(check)
    assert ten_men["under_review"] and engine.docs["ht-lead-lost-away-1"]["under_review"] and not brace["under_review"]
    pipeline.handle(stands)
    assert not any(d["under_review"] for d in engine.active())


def test_every_step_of_a_fact_is_audited_once(demo, history):
    """The audit trail has each step of every insight's history, in order, and nothing twice: the facts VAR took back are there, with whether they were on air."""
    from worker.desk import Desk
    from worker.writer import TemplateWriter

    stream, truth = demo
    sink = MemorySink()
    pipeline = run(stream, history, sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True))
    for d in pipeline.insights("demo-0007"):
        assert [e["action"] for e in sink.audit if e["card_id"] == d["id"]] == [h["action"] for h in d["history"]], d["key"]
    assert len({e["id"] for e in sink.audit}) == len(sink.audit)
    taken_back = {e["card_id"][len("insight-demo-0007-"):] for e in sink.audit if e["action"] == "superseded" and e["cause"] == "goal ruled out" and e["was_on_air"]}
    assert {"scorer-home-20-3", "comeback-home-2-2", "ht-lead-lost-away-1"} <= taken_back  # the three facts that counted the 63' goal, on air when VAR ruled it out


# --- who writes the facts, and the call budget --------------------------------------------------------------

class CountingWriter:
    """Stands in for the model: replies with the template texts, and records what it is asked to write."""

    name = "counting-model"

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def write_async(self, doc, facts, languages, note=None, avoid=None, duo=False, preferences=None):
        from worker.writer import TemplateWriter

        self.asked.append(doc.get("type", "card"))
        if doc.get("type") in ("insight", "storyline"):
            return TemplateWriter().write_insight(doc, facts[0], languages, duo=duo)
        return TemplateWriter().write(doc, facts[0], languages, duo=duo)


def test_live_the_model_writes_the_cards_the_templates_write_the_facts(demo, history):
    from worker.desk import Desk
    from worker.writer import TemplateWriter

    sink, model = MemorySink(), CountingWriter()
    desk = Desk(sink, model, auto_approve=True, insight_writer=TemplateWriter())
    pipeline = run(demo[0], history, sink=sink, desk=desk)
    written = [d for d in pipeline.insights("demo-0007") if "texts" in d]
    assert written and all(d["writer"] == "template" for d in written)
    assert set(model.asked) == {"card"} and 12 <= len(model.asked) <= 24  # 11 cards and a rewrite, a retry at most each
    assert desk.calls["demo-0007"] == len(model.asked) <= desk.max_calls_per_match == 40


def test_the_facts_go_to_the_model_only_when_asked(demo, history):
    from worker.desk import Desk

    sink, model = MemorySink(), CountingWriter()
    run(demo[0], history, sink=sink, desk=Desk(sink, model, auto_approve=True))
    assert "insight" in model.asked and len(model.asked) <= 40


def test_without_history_the_pipeline_is_unchanged(demo):
    pipeline = Pipeline()
    for m in demo[0]:
        pipeline.handle(m)
    assert pipeline.engines == {} and pipeline.sink.insights == {}


def test_json_file_sink_and_threaded_sink(demo, history, tmp_path):
    run(demo[0], history, sink=JsonFileSink(tmp_path))
    stored = json.loads((tmp_path / "demo-0007" / "insights.json").read_text(encoding="utf-8"))
    assert any(d["key"] == "scorer-home-20-4" and d["status"] == "proposed" for d in stored)
    inner = MemorySink()
    sink = ThreadedSink(inner)
    run(demo[0], history, sink=sink)
    assert sink.close() == []
    assert inner.insights["insight-demo-0007-scorer-home-20-3"]["status"] == "superseded"
    assert inner.insights["insight-demo-0007-stat-of-the-match"]["headline_facts"]["top_fact"] == "scorer-home-20-4"


class TextDesk:
    """A desk with the methods the pipeline calls, write_insight included."""

    def __init__(self) -> None:
        self.written = []

    def write(self, card, teams):
        return card

    def on_action(self, card, action, teams):
        return card

    def write_insight(self, doc, teams):
        self.written.append(doc["key"])
        doc["texts"] = [{"lang": "en", "headline": doc["key"], "why": "", "claims": []}]
        doc["status"] = "pending"
        return doc


def test_insights_go_through_the_desk_when_it_can_write_them(demo, history):
    desk = TextDesk()
    pipeline = run(demo[0], history, desk=desk)
    docs = {d["key"]: d for d in pipeline.insights("demo-0007")}
    assert "scorer-home-20-4" in desk.written and docs["scorer-home-20-4"]["status"] == "pending"
    assert pipeline.sink.insights["insight-demo-0007-scorer-home-20-4"]["texts"][0]["headline"] == "scorer-home-20-4"
    assert docs["scorer-home-20-3"]["status"] == "superseded"


def test_a_producer_decision_is_kept(demo, history):
    """The console writes decisions to the store; the engine reads them back before it changes an insight."""
    pipeline = run(demo[0], history)
    engine = pipeline.engines["demo-0007"]
    stored = pipeline.sink.insights["insight-demo-0007-scorer-home-20-4"]
    stored["status"], stored["texts"] = "rejected", [{"lang": "en", "headline": "edited", "why": "", "claims": []}]
    doc = engine.docs["scorer-home-20-4"]
    engine._refresh(doc)
    assert doc["status"] == "rejected" and doc["texts"][0]["headline"] == "edited"


def test_cli_loads_the_history_through_the_module(monkeypatch, tmp_path, history):
    import sys
    import types

    import league
    from worker.__main__ import load_history

    module = types.ModuleType("league.history")
    module.History = type("History", (), {"load": staticmethod(lambda path: history if path == tmp_path else None)})
    monkeypatch.setitem(sys.modules, "league.history", module)
    monkeypatch.setattr(league, "history", module, raising=False)
    assert load_history(tmp_path) is history
