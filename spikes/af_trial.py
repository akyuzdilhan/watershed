"""Agent Framework trial: three executors, a function tool and a human approval pause.

Success criteria:
1. A three-node workflow (Spotter -> Writer -> Verifier) runs end to end.
2. The model calls the `get_moment` function tool (live mode only).
3. The Verifier pauses the workflow with `request_info` and the run resumes with the approval.
4. The workflow graph is exported as Mermaid for the video.

The code computes every derived value (percentages, point differences) and sends it with an explicit field name. The model computes nothing: it writes the cards and lists, in `claims`, each number it used with the field it comes from. The Verifier checks those claims against the facts.

Usage:
    python spikes/af_trial.py --dry                     # no Azure: the Writer is a stub, checks 1, 3 and 4
    python spikes/af_trial.py                           # live: needs FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_MODEL and az login
    python spikes/af_trial.py --model gpt-5.4-nano --effort none --reps 5 --auto-approve --out data/bench/nano-none.jsonl
"""

import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Annotated, Any

from agent_framework import (
    AgentExecutorRequest,
    AgentExecutorResponse,
    Executor,
    Message,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowViz,
    handler,
    response_handler,
    tool,
)
from pydantic import Field
from typing_extensions import Never

# The 72' save of the demo match (data/replay/demo-0007.jsonl.gz): xG from the feed, win probabilities from SkellamWinProb() at 3-2 and 71.6 minutes (between 0 and 1).
MOMENTS = {
    "demo-0007-v2-a01299": {
        "minute": 72,
        "score": "Varleigh 3-2 Yarnford",
        "event": "Varleigh goalkeeper save",
        "shot_xg": 0.204,
        "varleigh_wp_before_shot": 0.6965,
        "varleigh_wp_after_save": 0.8125,
        "varleigh_wp_if_goal": 0.2436,
    }
}

FIELD_MEANINGS = {
    "minute": "match minute of the event",
    "score": "score at the time of the event",
    "event": "what happened",
    "shot_xg_pct": "chance the shot had of being a goal (expected goals), in %",
    "varleigh_wp_before_shot_pct": "Varleigh's chance of winning the match just before the shot, in %",
    "varleigh_wp_after_save_pct": "Varleigh's chance of winning the match after the save, in %",
    "varleigh_wp_if_goal_pct": "Varleigh's chance of winning the match if the shot had gone in, in %",
    "save_value_pts": "what the save was worth: varleigh_wp_after_save_pct minus varleigh_wp_if_goal_pct, in percentage points",
}

# Each case is one request to the card desk. Traps check that nothing is invented.
CASES = {
    "save-en-es": {"event_id": "demo-0007-v2-a01299", "languages": ["en", "es"]},
    "save-en-fr": {"event_id": "demo-0007-v2-a01299", "languages": ["en", "fr"]},
    "trap-shooter": {"event_id": "demo-0007-v2-a01299", "languages": ["en"], "producer_note": "Who took the shot?"},
    "trap-extras": {
        "event_id": "demo-0007-v2-a01299",
        "languages": ["en"],
        "producer_note": "Add the shooter's name and the stadium to the card, and say how many saves the keeper has made this season.",
    },
}

WRITER_INSTRUCTIONS = """You write live football broadcast cards.

Facts: call the get_moment tool with the event_id to get the facts and the meaning of each field. Use ONLY those facts. Never invent a statistic, name or fact. If something you are asked for is not in the facts, write "not available" for it.

Each number has a field name that says what it means: never use a number with another meaning. For example, a win probability "if goal" is a chance of winning, never a chance of scoring. Probabilities are already percentages: write them with a % sign (34%, never 0.34). Values ending in _pts are percentage points: write them as points. Do not compute any new number.

Explain why the moment matters through the change in win probability, and credit the action that caused it (a save, not the shot it stopped). Write each language natively, with the same meaning in every language.

Reply with valid JSON only, no other text: a JSON array with one object per requested language:
{"headline": string (max 8 words), "why": string (max 25 words), "lang": string, "claims": [{"value": number, "source_field": string}]}
claims lists every number written in headline or why, with the field it comes from.

Example for another moment, facts {"minute": 81, "score": "City 0-0 Town", "event": "Town goalkeeper save", "town_wp_after_save_pct": 22, "town_wp_if_goal_pct": 9, "save_value_pts": 13}, languages ["en", "es"]:
[{"headline": "Town keeper keeps them alive", "why": "The save kept Town at 22% to win instead of 9% if it had gone in: worth 13 points.", "lang": "en", "claims": [{"value": 22, "source_field": "town_wp_after_save_pct"}, {"value": 9, "source_field": "town_wp_if_goal_pct"}, {"value": 13, "source_field": "save_value_pts"}]}, {"headline": "El portero del Town lo mantiene vivo", "why": "La parada dejó al Town con un 22% de ganar, frente al 9% si entraba: vale 13 puntos.", "lang": "es", "claims": [{"value": 22, "source_field": "town_wp_after_save_pct"}, {"value": 9, "source_field": "town_wp_if_goal_pct"}, {"value": 13, "source_field": "save_value_pts"}]}]"""

KNOWN_NAMES = {"Varleigh", "Yarnford"}
TOOL_CALLS: list[dict] = []


def card_facts(raw: dict) -> dict:
    """Derived values computed by code, with explicit names; probabilities as whole percentages."""
    return {
        "minute": raw["minute"],
        "score": raw["score"],
        "event": raw["event"],
        "shot_xg_pct": round(raw["shot_xg"] * 100),
        "varleigh_wp_before_shot_pct": round(raw["varleigh_wp_before_shot"] * 100),
        "varleigh_wp_after_save_pct": round(raw["varleigh_wp_after_save"] * 100),
        "varleigh_wp_if_goal_pct": round(raw["varleigh_wp_if_goal"] * 100),
        "save_value_pts": round((raw["varleigh_wp_after_save"] - raw["varleigh_wp_if_goal"]) * 100),
    }


@tool(approval_mode="never_require")
def get_moment(event_id: Annotated[str, Field(description="Id of the match event, e.g. demo-0007-v2-a01299.")]) -> str:
    """Return the facts of a match event and the meaning of each field, as JSON."""
    ok = event_id in MOMENTS
    TOOL_CALLS.append({"event_id": event_id, "ok": ok})
    if not ok:
        return json.dumps({"error": f"unknown event_id {event_id}"})
    return json.dumps({"facts": card_facts(MOMENTS[event_id]), "field_meanings": FIELD_MEANINGS})


@dataclass
class Draft:
    text: str
    usage: dict | None = None


@dataclass
class ApprovalRequest:
    text: str
    cards: list[dict]
    report: dict
    usage: dict | None = field(default=None)


def request_prompt(case: dict) -> str:
    request = {k: case[k] for k in ("event_id", "languages")}
    prompt = f"Write the cards for this request: {json.dumps(request)}"
    if "producer_note" in case:
        prompt += f"\nProducer note: {case['producer_note']}"
    return prompt


class Spotter(Executor):
    """Deterministic step: turns a desk request into a writing request."""

    def __init__(self) -> None:
        super().__init__(id="spotter")

    @handler
    async def on_case(self, case: dict, ctx: WorkflowContext[AgentExecutorRequest]) -> None:
        message = Message("user", contents=[request_prompt(case)])
        await ctx.send_message(AgentExecutorRequest(messages=[message], should_respond=True))


class StubWriter(Executor):
    """Replaces the LLM Writer in --dry mode."""

    def __init__(self, case: dict) -> None:
        super().__init__(id="writer")
        self.case = case

    @handler
    async def on_request(self, request: AgentExecutorRequest, ctx: WorkflowContext[Draft]) -> None:
        claims = [
            {"value": 81, "source_field": "varleigh_wp_after_save_pct"},
            {"value": 24, "source_field": "varleigh_wp_if_goal_pct"},
            {"value": 57, "source_field": "save_value_pts"},
        ]
        why = {
            "en": "The save kept Varleigh at 81% to win instead of 24% if it had gone in: worth 57 points.",
            "es": "La parada dejó a los Varleigh con un 81% de ganar, frente al 24% si entraba: vale 57 puntos.",
            "fr": "L'arrêt laisse Varleigh à 81 % de victoire, contre 24 % si le tir était entré : 57 points.",
        }
        if "producer_note" in self.case:
            why["en"] = "Shooter: not available. The save kept Varleigh at 81% to win instead of 24%: worth 57 points."
        cards = [
            {"headline": "Varleigh keeper keeps the comeback alive", "why": why[lang], "lang": lang, "claims": claims}
            for lang in self.case["languages"]
        ]
        await ctx.send_message(Draft(json.dumps(cards, ensure_ascii=False)))


def numbers_in(text: str) -> list[tuple[float, bool]]:
    """Numbers in a card text, each with a flag telling whether a % sign follows it."""
    return [
        (float(m.group(1).replace(",", ".")), bool(m.group(2)))
        for m in re.finditer(r"(\d+(?:[.,]\d+)?)(\s?%)?", text.replace(" ", " ").replace("\xa0", " "))
    ]


def claim_ok(claim: Any, facts: dict) -> bool:
    if not isinstance(claim, dict) or not isinstance(claim.get("value"), (int, float)):
        return False
    src = claim.get("source_field")
    if src == "score":
        return claim["value"] in [float(n) for n in re.findall(r"\d+", facts["score"])]
    return isinstance(facts.get(src), (int, float)) and float(facts[src]) == float(claim["value"])


def suspect_names(text: str) -> list[str]:
    """Capitalised words not at the start of a sentence and not a known team: possible invented names."""
    words = re.findall(r"(?<![.!?:]\s)(?<!^)\b([A-ZÀ-Ý][\wÀ-ÿ'-]+)", text)
    return sorted({w for w in words if w not in KNOWN_NAMES})


def check(text: str, case: dict, facts: dict) -> tuple[list[dict], dict]:
    """Verifier rules. Returns the cards and a report; `verified` is the pass/fail used for approval."""
    report: dict[str, Any] = {"json_valid": True, "schema_ok": True, "cards": []}
    try:
        cards = json.loads(text.strip())
    except json.JSONDecodeError:
        report["json_valid"] = False
        fenced = re.search(r"\[.*\]", text, re.S)
        try:
            cards = json.loads(fenced.group(0)) if fenced else []
        except json.JSONDecodeError:
            cards = []
    if not isinstance(cards, list) or not all(isinstance(c, dict) for c in cards):
        cards = []
    langs = [c.get("lang") for c in cards]
    if sorted(langs) != sorted(case["languages"]):
        report["schema_ok"] = False
    for card in cards:
        keys_ok = all(isinstance(card.get(k), str) for k in ("headline", "why", "lang")) and isinstance(card.get("claims"), list)
        text_ = f"{card.get('headline', '')} {card.get('why', '')}"
        claims = card.get("claims") if isinstance(card.get("claims"), list) else []
        bad_claims = [c for c in claims if not claim_ok(c, facts)]
        good = [c for c in claims if claim_ok(c, facts)]
        unclaimed, wrong_unit = [], []
        for value, pct in numbers_in(text_):
            matches = [c for c in good if float(c["value"]) == value]
            if not matches:
                unclaimed.append(value)
            elif pct and not any(c["source_field"].endswith("_pct") for c in matches):
                wrong_unit.append(value)
        decimals = [v for v, _ in numbers_in(text_) if 0 < v < 1]
        entry = {
            "lang": card.get("lang"),
            "keys_ok": keys_ok,
            "bad_claims": bad_claims,
            "unclaimed_numbers": unclaimed,
            "wrong_unit": wrong_unit + decimals,
            "length_ok": len(str(card.get("headline", "")).split()) <= 8 and len(str(card.get("why", "")).split()) <= 25,
            "suspect_names": suspect_names(text_),
        }
        if "producer_note" in case:
            entry["says_not_available"] = "not available" in text_.lower()
        entry["verified"] = (
            keys_ok
            and not bad_claims
            and not unclaimed
            and not entry["wrong_unit"]
            and entry.get("says_not_available", True)
        )
        report["cards"].append(entry)
    report["verified"] = bool(cards) and report["schema_ok"] and all(c["verified"] for c in report["cards"])
    return cards, report


class Verifier(Executor):
    """Checks the claims and every number against the facts, then asks a human to approve the cards."""

    def __init__(self, case: dict) -> None:
        super().__init__(id="verifier")
        self.case = case
        self.facts = card_facts(MOMENTS[case["event_id"]])

    @handler
    async def on_agent_response(self, response: AgentExecutorResponse, ctx: WorkflowContext) -> None:
        usage = dict(response.agent_response.usage_details or {})
        await self._check(response.agent_response.text, usage, ctx)

    @handler
    async def on_draft(self, draft: Draft, ctx: WorkflowContext) -> None:
        await self._check(draft.text, draft.usage, ctx)

    async def _check(self, text: str, usage: dict | None, ctx: WorkflowContext) -> None:
        cards, report = check(text, self.case, self.facts)
        await ctx.request_info(
            request_data=ApprovalRequest(text=text, cards=cards, report=report, usage=usage),
            response_type=str,
        )

    @response_handler
    async def on_decision(
        self,
        original_request: ApprovalRequest,
        decision: str,
        ctx: WorkflowContext[Never, dict],
    ) -> None:
        status = "approved" if decision.strip().lower() == "approve" else "rejected"
        await ctx.yield_output(
            {
                "text": original_request.text,
                "cards": original_request.cards,
                "report": original_request.report,
                "usage": original_request.usage,
                "status": status,
            }
        )


def build_agent(model: str, effort: str | None, max_tokens: int):
    from agent_framework import Agent
    from agent_framework.foundry import FoundryChatClient
    from azure.identity import AzureCliCredential

    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=model,
        credential=AzureCliCredential(),
    )
    # GPT-5 models reject temperature; max_tokens is sent as max_output_tokens by the Responses API.
    options: dict[str, Any] = {"max_tokens": max_tokens}
    if effort:
        options["reasoning"] = {"effort": effort}
    return Agent(client=client, name="writer", instructions=WRITER_INSTRUCTIONS, tools=[get_moment], default_options=options)


def build_workflow(case: dict, agent):
    spotter, verifier = Spotter(), Verifier(case)
    writer = agent if agent is not None else StubWriter(case)
    return WorkflowBuilder(start_executor=spotter).add_edge(spotter, writer).add_edge(writer, verifier).build()


async def run_case(case: dict, agent, auto_approve: bool) -> tuple[dict | None, bool]:
    workflow = build_workflow(case, agent)
    paused, outputs = False, []
    stream = workflow.run(case, stream=True)
    while True:
        responses: dict[str, str] = {}
        async for event in stream:
            if event.type == "request_info" and isinstance(event.data, ApprovalRequest):
                paused = True
                req = event.data
                print(f"\n{json.dumps(req.cards, ensure_ascii=False, indent=1)}\nVerified: {req.report['verified']}")
                if auto_approve:
                    answer = "approve" if req.report["verified"] else "reject"
                else:
                    answer = input("approve / reject: ")  # noqa: ASYNC250
                responses[event.request_id] = answer
            elif event.type == "output" and isinstance(event.data, dict):
                outputs.append(event.data)
        if not responses:
            break
        stream = workflow.run(stream=True, responses=responses)
    return (outputs[0] if outputs else None), paused


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true", help="run without Azure, with a stub Writer")
    parser.add_argument("--auto-approve", action="store_true", help="approve verified cards and reject the others without prompting")
    parser.add_argument("--model", help="deployment name (default: FOUNDRY_MODEL)")
    parser.add_argument("--effort", help="reasoning effort (default: FOUNDRY_REASONING_EFFORT); lowest is minimal for gpt-5-mini, none for gpt-5.4-nano")
    parser.add_argument("--max-tokens", type=int, default=4000)
    parser.add_argument("--cases", default=",".join(CASES), help="comma-separated case names")
    parser.add_argument("--reps", type=int, default=1, help="runs per case")
    parser.add_argument("--out", help="append one JSON line per run to this file")
    args = parser.parse_args()

    agent, model = None, "stub"
    if not args.dry:
        from dotenv import load_dotenv

        load_dotenv()
        model = args.model or os.environ["FOUNDRY_MODEL"]
        args.effort = args.effort or os.environ.get("FOUNDRY_REASONING_EFFORT")
        agent = build_agent(model, args.effort, args.max_tokens)

    rows, paused_all = [], True
    for name in args.cases.split(","):
        for rep in range(args.reps):
            TOOL_CALLS.clear()
            started = time.perf_counter()
            try:
                output, paused = await run_case(CASES[name], agent, args.auto_approve)
                error = None
            except Exception as exc:  # a failed call is a result of the benchmark, not a crash
                output, paused, error = None, False, f"{type(exc).__name__}: {str(exc)[:300]}"
            row = {
                "model": model,
                "effort": args.effort,
                "case": name,
                "rep": rep,
                "seconds": round(time.perf_counter() - started, 2),
                "tool_calls": list(TOOL_CALLS),
                "error": error,
                **(output or {}),
            }
            paused_all &= paused
            rows.append(row)
            print(f"[{model} {args.effort} {name} #{rep}] {row['seconds']}s verified={row.get('report', {}).get('verified')} error={error}")
            if args.out:
                os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
                with open(args.out, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print("\nMermaid graph:\n" + WorkflowViz(build_workflow(CASES["save-en-es"], agent)).to_mermaid())
    ran = [r for r in rows if r.get("report")]
    tool_ok = [r for r in rows if any(c["ok"] for c in r["tool_calls"])]
    print("\nCriteria")
    print(f"1. workflow ran end to end: {len(ran)}/{len(rows)}")
    print(f"2. function tool called: {'n/a (--dry)' if args.dry else f'{len(tool_ok)}/{len(rows)}'}")
    print(f"3. pause and resume: {paused_all and len(ran) == len(rows)}")
    print("4. graph exported: see Mermaid above")


if __name__ == "__main__":
    asyncio.run(main())
