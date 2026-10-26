"""The Writer reading its facts through the desk's MCP server.

`FoundryWriter` gives the model a local function tool, `get_moment`. `McpWriter` serves the same tool from the desk's read-only MCP server (`worker.mcp_server`, in process), so the facts a model reads come from one place that any MCP host can also open. The contract is unchanged: same instructions, same message, same tool name, description and JSON, same time limit, tool loop, Verifier, retry, template and budget. Selected with `python -m worker ... --writer foundry --facts mcp`.
"""

import contextlib
import json
import logging
from collections import Counter
from typing import Any

from agent_framework import MCPStdioTool, tool
from mcp import Client

from worker.mcp_server import FactRegistry, build_server
from worker.writer import MODEL_TIMEOUT_S, FoundryWriter, foundry_client, writer_message

__all__ = ["McpWriter", "foundry_client"]

log = logging.getLogger(__name__)


class McpWriter(FoundryWriter):
    """FoundryWriter with get_moment read through the desk's MCP server (worker.mcp_server): same instructions, message, tool name and JSON, same time limit,
    Verifier, retry, template and budget; only the transport changes. If the in-process connection cannot open, the same get_moment is served by a local
    function tool, and the fallback is logged. `client` replaces the Foundry chat client (tests)."""

    def __init__(self, registry: FactRegistry, model: str | None = None, effort: str | None = None, max_tokens: int | None = None, client: Any = None,
                 timeout_s: float = MODEL_TIMEOUT_S) -> None:
        self.registry = registry
        self.server = build_server(registry)  # the desk's tools only: a history call would add a model round trip to the card
        self.transports: Counter = Counter()  # "mcp" or "local", per write
        self._client = client

        @tool(name="get_moment", approval_mode="never_require")
        def get_moment(card_id: str) -> str:
            """Return the facts of a card and the meaning of each field, as JSON."""
            return registry.moment(card_id) or json.dumps({"error": f"unknown card_id {card_id}"})

        self._local_tool = get_moment
        super().__init__(model, effort, max_tokens, timeout_s=timeout_s)

    def agent_tools(self) -> list:
        return []  # each run brings its get_moment, through MCP or local

    def _foundry_agent(self, client: Any = None):
        return super()._foundry_agent(client or self._client)

    async def write_async(self, card: dict, facts: tuple[dict, dict], languages: list[str], note: str | None = None, avoid: dict | None = None, duo: bool = False,
                          preferences: str | None = None, max_tokens: int | None = None) -> str:
        """One Writer call, as FoundryWriter.write_async. The connection opens and closes in this task (anyio requires it), so nothing is left on the desk's loop; a
        connection that fails to close after the reply is logged, and the reply is kept."""
        self.registry.put_moment(card["id"], *facts)
        message = writer_message(card, languages, note, avoid, duo, preferences)
        async with contextlib.AsyncExitStack() as stack:
            tools, transport = [self._local_tool], "local"
            try:
                client = await stack.enter_async_context(Client(self.server, read_timeout_seconds=5))
                tools = [await stack.enter_async_context(MCPStdioTool("desk", mcp_client=client, allowed_tools=["get_moment"], load_prompts=False,
                                                                      approval_mode="never_require"))]
                transport = "mcp"
            except Exception as error:
                log.warning("get_moment served locally, MCP unavailable: %r", error)
            self.transports[transport] += 1
            reply = await self.run(message, max_tokens, tools=tools)
            opened = stack.pop_all()  # closed below, so that a close error cannot cost the reply
        try:
            await opened.aclose()
        except Exception as error:
            log.warning("MCP connection not closed cleanly, reply kept: %r", error)
        return reply
