"""The Writer with get_moment read through the desk's MCP server (worker.mcp_writer), with a scripted chat client: no model, no network."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import worker.mcp_writer
from scripted_chat import ScriptedChatClient, call
from worker.desk import Desk
from worker.mcp_server import FactRegistry
from worker.mcp_writer import McpWriter
from worker.pipeline import MemorySink
from worker.writer import TemplateWriter, draft, writer_message

LANGS = ["en", "fr", "es"]
TEAMS = {"home": "Varleigh", "away": "Yarnford"}
SRC = Path(__file__).resolve().parents[1] / "src"


def make_card(id_="card-e1", minute="52'"):
    return {"id": id_, "version": 1, "match_id": "m", "team": "home", "team_name": "Varleigh", "moment": "goal", "minute_label": minute, "clock_s": 3100.0,
            "player": {"id": "home-9", "name": "Elio Ingleby"}, "score_before": {"home": 0, "away": 2}, "facts": {"team_wp_before_pct": 30, "team_wp_after_pct": 54},
            "value_pts": 24}


def facts_from_result(messages) -> dict:
    """The facts the model was shown by its get_moment call."""
    results = [c.result for m in messages for c in m.contents if c.type == "function_result"]
    return json.loads(results[-1])["facts"]


def scripted_writer(card, registry):
    """A Writer whose model calls get_moment, then writes the template texts from the facts it was shown."""
    client = ScriptedChatClient([call("get_moment", card_id=card["id"]), lambda msgs: TemplateWriter().write(card, facts_from_result(msgs), LANGS)])
    return McpWriter(registry, model="scripted", client=client), client


def test_the_writer_reads_get_moment_through_mcp():
    registry = FactRegistry()
    card = make_card()
    w, client = scripted_writer(card, registry)
    d = asyncio.run(draft(card, TEAMS, w, LANGS))
    assert (d.writer, d.attempts, d.report["verified"]) == ("scripted", 1, True)
    assert registry.served["get_moment"] == 1 and w.transports == {"mcp": 1}  # fails when MCPStdioTool cannot connect (tenacity missing, for one)
    first = client.requests[0]
    assert [text for role, _, text in first["messages"] if role == "user"] == [writer_message(card, LANGS)]
    assert first["tools"] == ["get_moment"]
    assert client.requests[1]["results"] == [registry.moment(card["id"])]


def test_the_model_sees_the_same_tool_either_way():
    from agent_framework import MCPStdioTool
    from mcp import Client

    w = McpWriter(FactRegistry(), model="scripted", client=ScriptedChatClient([]))

    async def through_mcp():
        async with Client(w.server) as client, MCPStdioTool("desk", mcp_client=client, allowed_tools=["get_moment"], load_prompts=False,
                                                            approval_mode="never_require") as desk:
            return desk.functions[0]

    remote, local = asyncio.run(through_mcp()), w._local_tool
    assert (remote.name, remote.description) == (local.name, local.description) == ("get_moment", "Return the facts of a card and the meaning of each field, as JSON.")
    untitled = lambda schema: {k: v for k, v in schema.items() if k != "title"}  # the schema's title is the only difference
    assert untitled(remote.parameters()) == untitled(local.parameters())


def test_the_desk_loop_keeps_no_task_after_two_cards():
    registry = FactRegistry()
    cards = [make_card("card-a", "52'"), make_card("card-b", "57'")]
    steps = []
    desk = None

    def reply(c):
        return lambda msgs: TemplateWriter().write(c, facts_from_result(msgs), LANGS, memory=desk.memory("m"), duo=True)

    for c in cards:
        steps += [call("get_moment", card_id=c["id"]), reply(c)]
    w = McpWriter(registry, model="scripted", client=ScriptedChatClient(steps))
    desk = Desk(MemorySink(), w)
    for c in cards:
        out = desk.write(c, TEAMS)
        assert (out["writer"], out["duo_writer"], out["verification"]["verified"], out["verification"]["attempts"]) == ("scripted", "scripted", True, 1)
    assert registry.served["get_moment"] == 2 and w.transports == {"mcp": 2}
    assert desk.calls == {"m": 2}
    assert not [t for t in asyncio.all_tasks(desk.loop) if not t.done()]


class Boom:
    """An MCP client that cannot connect."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        raise ConnectionError("no MCP today")

    async def __aexit__(self, *exc) -> bool:
        return False


def test_without_mcp_the_same_tool_is_served_locally(monkeypatch):
    card = make_card()
    w, _ = scripted_writer(card, FactRegistry())
    through_mcp = asyncio.run(draft(card, TEAMS, w, LANGS))
    monkeypatch.setattr(worker.mcp_writer, "Client", Boom)
    registry = FactRegistry()
    w, client = scripted_writer(card, registry)
    local = asyncio.run(draft(card, TEAMS, w, LANGS))
    assert local.texts == through_mcp.texts and (local.writer, local.attempts) == ("scripted", 1)
    assert w.transports == {"local": 1} and registry.served["get_moment"] == 0
    assert client.requests[1]["results"] == [registry.moment(card["id"])]


def test_the_default_path_imports_none_of_it():
    code = ("import sys, worker.__main__, worker.desk, worker.pipeline; print(sorted(m for m in ('mcp', 'agent_framework_orchestrations', 'worker.mcp_server', "
            "'worker.duo_agents', 'worker.agent_desk', 'worker.mcp_writer') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env={**os.environ, "PYTHONPATH": str(SRC)}).stdout
    assert out.strip() == "[]"
