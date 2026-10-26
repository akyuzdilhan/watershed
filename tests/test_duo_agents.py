"""The commentary duo by two agents (worker.duo_agents): the running order, the Verifier at each turn, the ledger of tool facts, the budget and the runner. Scripted or dry chat clients only: no model, no network."""

import asyncio
import dataclasses
import json
import threading
import time

import pytest

from agent_framework import AgentResponse, Content, Message
from generator import generate_match
from league.history import generate
from scripted_chat import ScriptedModel, call, turn
from worker.desk import Desk
from worker.duo import PERSONAS
from worker.duo_agents import (DUO_RULES, DuoBudget, DuoJob, DuoModel, DuoRunner, duo_plan, duo_spec, duo_workflow, line_problems, persona_instructions, relied_on,
                               run_duo, unbacked_number_words, unknown_names)
from worker.mcp_server import FactRegistry, HistoryFacts, build_server
from worker.pipeline import MemorySink, Pipeline
from worker.writer import EXAMPLE_DUO, EXAMPLE_FACTS, TemplateWriter, card_facts, verify_duo

LANGS = ["en", "fr", "es"]
TEAMS = {"home": "Varleigh", "away": "Yarnford"}
GOALS_FIELD = "player_marijn_scarpetta_league_goals"


@pytest.fixture(scope="module")
def hf():
    return HistoryFacts.build(generate(9, 5))


def goal_card(**extra) -> dict:
    """Varleigh go ahead at 66' through Marijn Scarpetta's third goal, from 30% to 54%: wren, joss, wren."""
    card = {"id": "card-g66", "version": 1, "match_id": "m", "team": "home", "team_name": "Varleigh", "moment": "goal", "minute_label": "66'", "clock_s": 3960.0,
            "player": {"id": "home-20", "name": "Marijn Scarpetta"}, "score_before": {"home": 1, "away": 1},
            "facts": {"team_wp_before_pct": 30, "team_wp_after_pct": 54}, "value_pts": 24, "scorer_match_goals": 3, "status": "approved"}
    return card | extra


def spec_of(card, tools=None):
    return duo_spec(card, TEAMS, LANGS, None, tools)


def run(spec, model, hf, budget=None):
    return asyncio.run(run_duo(spec, model, build_server(FactRegistry(), hf), budget or DuoBudget(36)))


def react():
    return turn(("en", "Marijn Scarpetta again! Varleigh believe now!"), ("fr", "Encore Marijn Scarpetta ! Varleigh y croit !"),
                ("es", "¡Otra vez Marijn Scarpetta! ¡El Varleigh cree!"))


def detail(goals):
    c = [{"value": goals, "source_field": GOALS_FIELD}]
    return turn(("en", f"Marijn Scarpetta had {goals} league goals before this season.", c), ("fr", f"Marijn Scarpetta avait {goals} buts en championnat avant cette saison.", c),
                ("es", f"Marijn Scarpetta tenía {goals} goles de liga antes de esta temporada.", c))


def answer():
    return turn(("en", "Six before this season, Joss, and three tonight!"), ("fr", "Six avant cette saison, Joss, et trois ce soir !"),
                ("es", "¡Seis antes de esta temporada, Joss, y tres esta noche!"))


def scarpetta_goals(hf) -> int:
    goals = hf.player("Marijn Scarpetta")[0][GOALS_FIELD]
    assert goals == 6  # the five-season history of the tests: Wren's "six" is backed by this fact
    return goals


# --- the exchange, as data -------------------------------------------------------------------------------

def test_the_running_order():
    facts, _ = card_facts(goal_card(scorer_match_goals=1), TEAMS)
    table = [{"speaker": "joss", "lang": "en", "line": "x", "claims": [{"value": 17, "source_field": "season_varleigh_rank_if_score_holds"}]}]
    assert [r for _, r in duo_plan(facts, table)] == ["react", "detail", "ask_table", "table"]
    assert len(duo_plan(facts | {"swing_pts": 51}, [])) == 3
    assert len(duo_plan(facts | {"scorer_match_goals": 3}, [])) == 3
    assert duo_plan(facts | {"swing_pts": 24}, []) == (("wren", "react"), ("joss", "detail"))


def test_persona_instructions_carry_the_voice_the_rules_and_a_turn_that_passes():
    for speaker, p in PERSONAS.items():
        text = persona_instructions(speaker, LANGS, ["get_match_state"])
        assert p.name in text and DUO_RULES in text and "get_match_state(card_id)" in text
        for lang in LANGS:
            assert p.voices[lang].sample in text and all(tic in text for tic in p.voices[lang].tics)
        for line in [l for l in EXAMPLE_DUO if l["speaker"] == speaker]:
            assert line_problems(line, EXAMPLE_FACTS) == [], line
    assert "no tools for this moment" in persona_instructions("joss", LANGS, [])


def test_number_words_need_their_fact():
    assert unbacked_number_words("That makes seventeen seasons.", "en", {"x": 5}) == ["seventeen"]
    assert unbacked_number_words("That is three for him.", "en", {"scorer_match_goals": 3}) == []
    assert unbacked_number_words("A real six-pointer tonight.", "en", {"story_thread1_angle": "A relegation six-pointer: the result moves both clubs"}) == []
    assert unbacked_number_words("Ten men, and still going.", "en", {}) == []
    assert unbacked_number_words("Trois buts, et quinze saisons d'attente.", "fr", {"g": 3}) == ["quinze"]
    assert unbacked_number_words("¡Tres goles!", "es", {"g": 3}) == []


@pytest.mark.parametrize("lang, line, said", [
    ("es", "¡Su tercer gol de la noche para el Varleigh!", ["tercer"]),
    ("es", "Es la tercera vez que marca Scarpetta.", ["tercera"]),
    ("en", "Scarpetta completes his hat-trick!", ["hat-trick"]),
    ("fr", "Le triplé pour Scarpetta !", ["triplé"]),
    ("fr", "Scarpetta réussit le coup du chapeau !", ["hat-trick"]),
    ("es", "¡Triplete de Scarpetta!", ["triplete"]),
])
def test_feats_and_spanish_ordinals_need_their_fact(lang, line, said):
    facts, _ = card_facts(goal_card(scorer_match_goals=2), TEAMS)
    assert 3 not in facts.values()
    assert unbacked_number_words(line, lang, facts) == said
    assert unbacked_number_words(line, lang, facts | {"scorer_match_goals": 3}) == []


def test_a_brace_needs_a_two():
    facts, _ = card_facts(goal_card(score_before={"home": 0, "away": 0}, scorer_match_goals=1), TEAMS)
    assert 2 not in facts.values()
    for lang, line, word in [("en", "A brace for Scarpetta!", "brace"), ("fr", "Le doublé pour Scarpetta !", "doublé"), ("es", "¡Doblete de Scarpetta!", "doblete")]:
        assert unbacked_number_words(line, lang, facts) == [word]
        assert unbacked_number_words(line, lang, facts | {"scorer_match_goals": 2}) == []


def test_a_number_in_several_words_is_read_whole():
    assert unbacked_number_words("Dix-sept saisons sans titre.", "fr", {"g": 7}) == ["dix-sept"]
    assert unbacked_number_words("Dix-sept saisons sans titre.", "fr", {"g": 17}) == []
    assert unbacked_number_words("Vingt et un buts avant cette saison.", "fr", {"g": 21}) == []
    assert unbacked_number_words("Quatre-vingt-dix minutes de folie.", "fr", {"g": 90}) == []
    assert unbacked_number_words("Twenty-two goals before this season.", "en", {"g": 20}) == ["twenty-two"]
    assert unbacked_number_words("Treinta y dos goles antes de esta temporada.", "es", {"g": 32}) == []
    assert unbacked_number_words("Veintidós goles antes de esta temporada.", "es", {"g": 2}) == ["veintidós"]
    assert unbacked_number_words("What a one-two between them!", "en", {}) == []  # a pass, not a number


def test_an_invented_name_is_an_error_for_the_agents():
    facts, _ = card_facts(goal_card(), TEAMS)
    invented = {"lang": "en", "line": "Even Marco Pellegrini would have been proud of that.", "claims": []}
    assert any(": unknown_names: " in p for p in line_problems(invented, facts))
    ours = {"lang": "en", "line": "Joss and Wren have seen nothing like it.", "claims": []}
    assert line_problems(ours, facts) == []


@pytest.mark.parametrize("lang, line, names", [
    ("en", "Pellegrini would have buried that too.", ["Pellegrini"]),
    ("en", "Scarpetta again: Shearer would be proud.", ["Shearer"]),
    ("en", "Varleigh lead, Wren. Haaland himself would take that.", ["Haaland"]),
    ("es", "¡Qué gol! Pellegrini no lo habría hecho mejor.", ["Pellegrini"]),
    ("fr", "Quel but ! Pellegrini n'aurait pas fait mieux.", ["Pellegrini"]),
    ("en", "Even Wren is lost for words. What a goal!", []),
    ("fr", "Encore Scarpetta ! Quelle soirée pour Varleigh. C'est lui, encore lui.", []),
    ("es", "¡Hasta el Varleigh se lo cree! Otra vez Scarpetta.", []),
    ("en", "Scarpetta's night, Joss. Seventeen minutes to go.", []),
])
def test_a_name_at_the_start_of_a_sentence_is_checked_too(lang, line, names):
    facts, _ = card_facts(goal_card(), TEAMS)
    assert unknown_names(line, lang, facts) == names
    problems = line_problems({"lang": lang, "line": line, "claims": []}, facts)
    assert any(": unknown_names: " in p for p in problems) == bool(names)


def test_a_name_from_a_tool_result_may_start_a_sentence():
    facts = card_facts(goal_card(), TEAMS)[0] | {"last_hat_trick_by_substitute_varleigh_player": "Dario Delacroix"}
    assert unknown_names("Delacroix did it off the bench, Wren.", "en", facts) == []
    assert unknown_names("Delacroix did it off the bench, Wren.", "en", card_facts(goal_card(), TEAMS)[0]) == ["Delacroix"]


def test_a_tool_number_is_told_with_its_subject():
    facts = card_facts(goal_card(), TEAMS)[0] | {GOALS_FIELD: 6}
    subjects = {GOALS_FIELD: ["Marijn Scarpetta", "Varleigh"]}
    claim = [{"value": 6, "source_field": GOALS_FIELD}]
    alone = {"lang": "en", "line": "What a night. He had 6 league goals before this season.", "claims": claim}
    assert line_problems(alone, facts, subjects=subjects) == [f"en: subject: {GOALS_FIELD} is about Marijn Scarpetta or Varleigh: name it in the same sentence"]
    named = {"lang": "fr", "line": "Scarpetta avait 6 buts en championnat avant cette saison.", "claims": claim}
    assert line_problems(named, facts, subjects=subjects) == []
    assert line_problems(alone, facts) == []  # without subjects (a card fact), no rule


def test_a_last_time_without_a_club_is_told_about_its_club():
    field = "last_comeback_win_from_two_down_seasons_ago"
    facts = card_facts(goal_card(), TEAMS)[0] | {field: 6, "last_comeback_win_from_two_down_club": "Stellbury"}
    subjects = {field: ["Stellbury"]}  # last_time with no club: its subject is the club of that last time
    claim = [{"value": 6, "source_field": field}]
    league = {"lang": "en", "line": "The last comeback from two down in this league was 6 seasons ago.", "claims": claim}
    assert line_problems(league, facts, subjects=subjects) == [f"en: subject: {field} is about Stellbury: name it in the same sentence"]
    anchored = {"lang": "en", "line": "Stellbury were the last side to turn two goals around, 6 seasons ago.", "claims": claim}
    assert line_problems(anchored, facts, subjects=subjects) == []


def test_the_exchange_is_a_group_chat_of_two_agents():
    from agent_framework import Agent, WorkflowViz
    from scripted_chat import ScriptedChatClient

    job = DuoJob(spec_of(goal_card()), DuoBudget(36), "scripted")
    agents = [Agent(client=ScriptedChatClient([]), name=speaker) for speaker in ("wren", "joss")]
    graph = WorkflowViz(duo_workflow(agents, job)).to_mermaid()
    assert "wren" in graph and "joss" in graph and "group_chat_orchestrator" in graph


def test_the_table_turn_must_claim_a_season_fact():
    facts, _ = card_facts(goal_card(), TEAMS)
    line = {"lang": "en", "line": "Varleigh are flying now.", "claims": []}
    assert line_problems(line, facts, role="table") == ["en: table: claim at least one season_ field"]


# --- one exchange ------------------------------------------------------------------------------------------

def test_a_nominal_exchange(hf):
    goals = scarpetta_goals(hf)
    model = ScriptedModel([react(), answer()], [call("player_history", name="Marijn Scarpetta"), detail(goals)])
    card = goal_card()
    result = run(spec_of(card), model, hf)
    assert result.ok and result.reason is None
    facts, _ = card_facts(card, TEAMS)
    assert verify_duo(result.lines, LANGS, facts | {k: v["value"] for k, v in result.tool_facts.items()})["verified"]
    assert [l["speaker"] for l in result.lines if l["lang"] == "en"] == ["wren", "joss", "wren"]
    assert result.trace["runs"] == 3 and [t["speaker"] for t in result.trace["turns"]] == ["wren", "joss", "wren"]
    lookup = result.trace["turns"][1]["tools"][0]
    assert (lookup["name"], lookup["args"], lookup["error"]) == ("player_history", {"name": "Marijn Scarpetta"}, None) and GOALS_FIELD in lookup["fields"]
    assert result.tool_facts[GOALS_FIELD] == {"value": goals, "tool": "player_history", "args": {"name": "Marijn Scarpetta"}}
    second = [t for _, _, t in model.clients["wren"].requests[1]["messages"]]
    assert any(t.startswith("Joss said: ") and f"{goals} league goals" in t for t in second)
    assert sorted(model.clients["joss"].requests[0]["tools"]) == ["club_history", "head_to_head", "last_time", "player_history"]
    assert sorted(model.clients["wren"].requests[0]["tools"]) == ["get_match_state", "get_storyline"]


def test_a_rejected_line_is_retried_and_never_heard(hf):
    goals = scarpetta_goals(hf)
    model = ScriptedModel([react(), answer()], [call("player_history", name="Marijn Scarpetta"), detail(61), detail(goals)])  # 61: a bad claim
    result = run(spec_of(goal_card()), model, hf)
    assert result.ok and result.trace["runs"] == 4
    retry = " ".join(t for _, _, t in model.clients["joss"].requests[2]["messages"])
    assert "The Verifier rejected" in retry and "bad_claims" in retry
    assert result.trace["turns"][1]["attempts"] == 2 and result.trace["turns"][1]["rejected"]
    assert not any("61" in t for t in model.clients["wren"].texts())


def test_two_rejections_end_the_job(hf):
    model = ScriptedModel([react(), answer()], [call("player_history", name="Marijn Scarpetta"), detail(61), detail(61)])
    result = run(spec_of(goal_card()), model, hf)
    assert not result.ok and result.reason == "numbers" and result.lines == [] and result.tool_facts == {}
    assert len(model.clients["wren"].requests) == 1
    assert result.trace["turns"][-1]["verified"] is False


INVENTED = {  # a Joss line the Verifier refuses, and the reason the job then ends with
    "name": ("names", turn(("en", "Even Marco Pellegrini would envy Marijn Scarpetta tonight."), ("fr", "Même Marco Pellegrini envierait Marijn Scarpetta ce soir."),
                           ("es", "Hasta Marco Pellegrini envidiaría a Marijn Scarpetta esta noche."))),
    "name first": ("names", turn(("en", "Pellegrini would envy Marijn Scarpetta tonight."), ("fr", "Pellegrini envierait Marijn Scarpetta ce soir."),
                                 ("es", "¡Pellegrini envidiaría a Marijn Scarpetta esta noche!"))),
    "number word": ("numbers", turn(("en", "Seventeen seasons of waiting, and Varleigh lead."), ("fr", "Dix-sept saisons d'attente, et Varleigh mène."),
                                    ("es", "Diecisiete temporadas de espera, y el Varleigh manda."))),
    "number in words": ("numbers", turn(("en", "Twenty-two league goals for Marijn Scarpetta before this season."),
                                        ("fr", "Vingt-deux buts en championnat pour Marijn Scarpetta avant cette saison."),
                                        ("es", "Veintidós goles de liga para Marijn Scarpetta antes de esta temporada."))),
}


@pytest.mark.parametrize("case", sorted(INVENTED))
def test_an_invented_line_never_reaches_the_partner(hf, case):
    reason, line = INVENTED[case]
    model = ScriptedModel([react(), answer()], [line, line])
    result = run(spec_of(goal_card()), model, hf)
    assert not result.ok and result.reason == reason
    assert len(model.clients["joss"].requests) == 2 and len(model.clients["wren"].requests) == 1
    assert result.trace["turns"][1]["attempts"] == 2 and not result.trace["turns"][1]["verified"]


def test_a_table_turn_without_a_season_fact_ends_the_job(hf):
    season = {"facts": {"score_now": "Varleigh 2-1 Yarnford", "varleigh_rank_at_kickoff": 18, "varleigh_rank_if_score_holds": 17},
              "meanings": {"score_now": "the score these facts are computed for", "varleigh_rank_at_kickoff": "Varleigh' league position at kick-off",
                           "varleigh_rank_if_score_holds": "Varleigh' league position if the score holds"}}
    template = [{"speaker": "joss", "lang": "en", "line": "If it stays 2-1, Varleigh go up to 17th.", "claims": [{"value": 17, "source_field": "season_varleigh_rank_if_score_holds"}]}]
    card = goal_card(season_context=season, duo=template, scorer_match_goals=1)
    ask = turn(("en", "And the table, Joss?"), ("fr", "Et au classement, Joss ?"), ("es", "¿Y en la tabla, Joss?"))
    vague = turn(("en", "Varleigh are climbing, Wren."), ("fr", "Varleigh grimpe, Wren."), ("es", "El Varleigh sube, Wren."))
    c = [{"value": 30, "source_field": "varleigh_wp_before_pct"}, {"value": 54, "source_field": "varleigh_wp_after_pct"}]
    card_line = turn(("en", "Varleigh go from 30% to 54% to win.", c), ("fr", "Varleigh passe de 30 % à 54 % de chances de gagner.", c),
                     ("es", "El Varleigh pasa del 30% al 54% de opciones de ganar.", c))
    model = ScriptedModel([react(), ask], [card_line, vague, vague])
    result = run(spec_of(card), model, hf)
    assert [r for _, r in result.trace["plan"]] == ["react", "detail", "ask_table", "table"]
    assert not result.ok and result.reason == "table" and result.trace["runs"] == 5


def test_a_tool_fact_never_overrides_a_card_fact():
    spec = spec_of(goal_card())
    job = DuoJob(spec, DuoBudget(36), "scripted")
    payload = {"facts": {"swing_pts": 99, "match_score_now": "Varleigh 2-1 Yarnford"}, "field_meanings": {"match_score_now": "score now"}, "subject": ["Varleigh", "Yarnford"]}
    response = AgentResponse(messages=[Message(role="assistant", contents=[Content.from_function_call(call_id="c1", name="get_match_state", arguments={"card_id": "card-g66"})]),
                                       Message(role="tool", contents=[Content.from_function_result(call_id="c1", result=json.dumps(payload))])])
    (entry,) = job.absorb(response, 1)
    assert entry["name"] == "get_match_state" and entry["fields"] == ["match_score_now"]
    assert job.facts["swing_pts"] == 24 and "swing_pts" not in job.ledger
    assert job.conflicts == [{"field": "swing_pts", "value": 99, "kept": 24, "tool": "get_match_state"}]
    assert job.provenance["match_score_now"] == {"tool": "get_match_state", "args": {"card_id": "card-g66"}, "slot": 1}
    assert job.subjects["match_score_now"] == ["Varleigh", "Yarnford"]


def test_the_tool_facts_kept_are_those_the_lines_rely_on():
    ledger = {"player_x_clubs": "Gorsedale, Varleigh", "player_x_league_goals": 6, "player_x_appearances": 93, "history_through": "2025/26"}
    lines = [{"speaker": "joss", "lang": "en", "line": "Six of them, and it all started at Gorsedale.", "claims": []},
             {"speaker": "joss", "lang": "fr", "line": "Il compte 93 matchs.", "claims": [{"value": 93, "source_field": "player_x_appearances"}]}]
    assert relied_on(lines, ledger) == ["player_x_clubs", "player_x_league_goals", "player_x_appearances"]
    ledger = {"last_hat_trick_by_substitute_varleigh_player": "Dario Delacroix", "last_hat_trick_by_substitute_varleigh_match": "Varleigh 6-0 Foxmarsh"}
    possessive = [{"speaker": "joss", "lang": "en", "line": "Not since Delacroix's three off the bench.", "claims": []}]
    assert relied_on(possessive, ledger) == ["last_hat_trick_by_substitute_varleigh_player"]  # the name as names_ok reads it


def test_the_budget_stops_a_job_before_the_model(hf):
    model = ScriptedModel([react()], [])
    result = run(spec_of(goal_card()), model, hf, DuoBudget(1))
    assert not result.ok and result.reason == "budget"
    assert model.clients["joss"].requests == [] and result.trace["runs"] == 1


def test_a_job_reserves_its_turns_and_a_retry_takes_a_free_run():
    budget = DuoBudget(6)
    assert budget.reserve("m", ("a", 1), 4) and budget.left("m") == 2
    assert not budget.reserve("m", ("b", 1), 3) and budget.left("m") == 2  # refused: nothing held
    assert all(budget.take("m", ("a", 1)) for _ in range(4)) and budget.left("m") == 2
    assert budget.take("m", ("a", 1), retry=True) and budget.left("m") == 1
    assert budget.reserve("m", ("b", 1), 1) and budget.left("m") == 0
    assert not budget.take("m", ("a", 1), retry=True)  # b's run stays b's
    assert budget.take("m", ("b", 1)) and budget.used("m") == 6
    budget.reserve("m2", ("c", 1), 6)
    budget.release("m2", ("c", 1))
    assert budget.left("m2") == 6 and budget.left("m") == 0


def card_line():
    c = [{"value": 30, "source_field": "varleigh_wp_before_pct"}, {"value": 54, "source_field": "varleigh_wp_after_pct"}]
    return turn(("en", "Varleigh go from 30% to 54% to win.", c), ("fr", "Varleigh passe de 30 % à 54 % de chances de gagner.", c),
                ("es", "El Varleigh pasa del 30% al 54% de opciones de ganar.", c))


def test_a_queued_job_keeps_the_runs_it_reserved(hf):
    bad = turn(("en", "Varleigh go to 61% to win.", [{"value": 61, "source_field": "varleigh_wp_after_pct"}]), ("fr", "Varleigh passe à 61 % de chances de gagner."),
               ("es", "El Varleigh pasa al 61% de opciones de ganar."))
    runner = DuoRunner(ScriptedModel([react(), react()], [bad, card_line()]), FactRegistry(), hf, max_calls_per_match=4)
    for card_id in ("card-a", "card-b"):
        spec = spec_of(goal_card(id=card_id, scorer_match_goals=1))
        assert runner.budget.reserve("m", (card_id, 1), len(spec.plan))
        runner.submit(spec)
    assert runner.wait(10)
    results = {r.card_id: r for r in runner.results()}
    assert results["card-a"].reason == "numbers" and results["card-a"].trace["runs"] == 2  # its retry found no free run: card-b's two stayed card-b's
    assert results["card-b"].ok and runner.budget.used("m") == 4 and runner.budget.left("m") == 0
    runner.close()


def test_closing_the_runner_closes_the_model_client_on_the_duo_thread(hf):
    class Closing:
        def __init__(self) -> None:
            self.thread = None

        async def close(self) -> None:
            self.thread = threading.current_thread().name

    model = DuoModel(model="a-deployment")
    model._client = type("FoundryLike", (), {})()
    model._client.client, model._client.project_client = Closing(), Closing()
    parts = (model._client.client, model._client.project_client)
    runner = DuoRunner(model, FactRegistry(), hf)
    runner.close()
    assert [p.thread for p in parts] == ["duo-agents", "duo-agents"] and model._client is None
    asyncio.run(DuoModel(dry=True).aclose())  # no client: nothing to close


# --- the runner ------------------------------------------------------------------------------------------------

def test_a_model_error_is_a_reason(hf):
    runner = DuoRunner(ScriptedModel([RuntimeError("429 Too Many Requests (scripted)")], []), FactRegistry(), hf)
    runner.submit(spec_of(goal_card()))
    assert runner.wait(10)
    (result,) = runner.results()
    assert not result.ok and result.reason == "error:RuntimeError"
    runner.close()


def test_deadline_cancel_and_close(hf):
    runner = DuoRunner(ScriptedModel([react(), react()], [], delay=8.0), FactRegistry(), hf, deadline_s=0.2)
    start = time.perf_counter()
    runner.submit(spec_of(goal_card(id="card-a")))
    runner.submit(spec_of(goal_card(id="card-b")))
    runner.cancel("card-b")  # still queued behind card-a
    assert runner.wait(7)
    results = {r.card_id: r for r in runner.results()}
    assert results["card-a"].reason == "timeout" and results["card-b"].reason == "cancelled"
    assert time.perf_counter() - start < 7  # card-a's first request alone takes 8 s: the deadline cut it
    runner.close()
    assert not runner._thread.is_alive()
    runner.close()
    runner.submit(spec_of(goal_card(id="card-c")))  # after close: logged, not run
    assert runner.results() == []


def test_live_a_job_that_waited_too_long_is_dropped(hf):
    model = ScriptedModel([], [])
    runner = DuoRunner(model, FactRegistry(), hf, stale_after_s=0.5)
    runner.submit(dataclasses.replace(spec_of(goal_card()), submitted_at=time.monotonic() - 1.0))
    assert runner.wait(5)
    (result,) = runner.results()
    assert result.reason == "stale" and model.clients["wren"].requests == [] and runner.budget.used("m") == 0
    runner.close()


def test_the_dry_model_speaks_the_template_and_looks_the_player_up(hf):
    stream, _ = generate_match(7, "demo")
    sink = MemorySink()
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True))
    for m in stream:
        pipeline.handle(m)
    pipeline.flush()
    card = next(c for c in sink.cards.values() if c["minute_label"] == "66'")
    spec = spec_of(card)
    result = run(spec, DuoModel(dry=True), hf)
    assert result.ok, result.trace
    template = {l["line"] for l in card["duo"]}
    assert all(l["line"] in template for l in result.lines)
    lookups = [t for turn_ in result.trace["turns"] for t in turn_["tools"]]
    assert [(t["name"], t["args"]) for t in lookups] == [("player_history", {"name": "Marijn Scarpetta"})]
    assert result.trace["model"] == "dry"


@pytest.mark.parametrize("lang,text,claims,category", [
    ("en", "That is the 41st hat-trick in the league history.", [{"value": 41, "source_field": "hat_trick_count"}], "bad_claims"),
    ("en", "Forty-one hat-tricks in the league history before this one.", [], "number_words"),
    ("en", "Seen in 3 matches out of 19000 across the league.", [], "unclaimed_numbers"),
    ("en", "Offside by 4 cm, the flag stays down.", [], "unclaimed_numbers"),
    ("en", "City take three points if it ends now.", [], "style"),
    ("en", "What a save — unbelievable.", [], "style"),
    ("en", "Marco Pellegrini would have scored that.", [], "unknown_names"),
])
def test_the_on_air_rules_hold_for_the_agents(lang, text, claims, category):
    facts = dict(EXAMPLE_FACTS, hat_trick_count=41, hat_trick_seasons_with_one=30)
    problems = line_problems({"lang": lang, "speaker": "wren", "line": text, "claims": claims}, facts)
    assert any(p.split(": ")[1] == category for p in problems), problems


@pytest.mark.parametrize("lang,text", [("en", "Offside by a whisker, and Ari Holt can breathe."), ("fr", "Hors-jeu d'un rien."), ("es", "Fuera de juego por muy poco.")])
def test_an_offside_margin_in_words_is_fine(lang, text):
    assert line_problems({"lang": lang, "speaker": "joss", "line": text, "claims": []}, EXAMPLE_FACTS) == []
