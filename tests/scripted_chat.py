"""Scripted chat clients for the agent tests: the model's replies are written in the test, no model is called and nothing leaves the process. A helper module, not a test module."""

import asyncio
import json

from agent_framework import BaseChatClient, ChatResponse, Content, FunctionInvocationLayer, Message


class ScriptedChatClient(FunctionInvocationLayer, BaseChatClient):
    """Plays its steps in order, one per model request: a text reply, ("call", name, args) for a tool call, an exception to raise, or a callable (messages) -> text or ("call", ...). Records every request: messages as (role, author_name, text), tool_choice, tool names, and the tool results it was shown."""

    def __init__(self, steps, delay: float = 0.0, function_invocation_configuration=None) -> None:
        super().__init__(function_invocation_configuration=function_invocation_configuration)
        self.steps = list(steps)
        self.delay = delay
        self.requests: list[dict] = []

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        return self._reply(list(messages), dict(options))

    async def _reply(self, messages, options) -> ChatResponse:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.requests.append({"messages": [(m.role, m.author_name, m.text) for m in messages], "tool_choice": options.get("tool_choice"),
                              "tools": [getattr(t, "name", str(t)) for t in options.get("tools") or []],
                              "results": [c.result for m in messages for c in m.contents if c.type == "function_result"]})
        if not self.steps:
            raise RuntimeError("the script has no step left")
        step = self.steps.pop(0)
        if callable(step):
            step = step(messages)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, tuple):
            _, name, args = step
            return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call(call_id=f"call-{len(self.requests)}", name=name, arguments=args)])])
        return ChatResponse(messages=[Message(role="assistant", contents=[step])])

    def texts(self) -> list[str]:
        """Every message text of every request, for "did this reach the model" checks."""
        return [t for r in self.requests for _, _, t in r["messages"]]


class ScriptedModel:
    """The duo's model in tests (in place of worker.duo_agents.DuoModel): one ScriptedChatClient per speaker, with the duo's limits."""

    name = "scripted"

    def __init__(self, wren_steps=(), joss_steps=(), delay: float = 0.0) -> None:
        from worker.duo_agents import DUO_LIMITS

        self.clients = {"wren": ScriptedChatClient(wren_steps, delay, DUO_LIMITS), "joss": ScriptedChatClient(joss_steps, delay, DUO_LIMITS)}

    def client(self, speaker: str, spec) -> ScriptedChatClient:
        return self.clients[speaker]

    def options(self) -> dict:
        return {}


def call(tool: str, /, **args) -> tuple:
    """A tool call step: call("player_history", name="Marijn Scarpetta")."""
    return ("call", tool, args)


def turn(*lines) -> str:
    """A duo turn as the agents reply: {"lines": [...]}, each line a dict or (lang, text, claims)."""
    out = []
    for item in lines:
        if isinstance(item, dict):
            out.append(item)
        else:
            lang, text, *rest = item
            out.append({"lang": lang, "line": text, "claims": list(rest[0]) if rest else []})
    return json.dumps({"lines": out}, ensure_ascii=False)
