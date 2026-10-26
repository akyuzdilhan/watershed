# The agentic prototype: what it does, how to run it, how to test it, its limits

A local prototype, behind options, for criterion 2 of the judging (agentic design: MCP integration, multi-agent collaboration). The design, the budget and the latency are in `docs/notes/agents.md`; the fields and the tools are in `docs/contracts.md` ("Agents and MCP").

## What it does

| Piece | Module | What it adds |
|---|---|---|
| The desk's MCP server, `watershed-desk` | `src/worker/mcp_server.py` | seven read-only tools: the facts of a card (`get_moment`), the match so far (`get_match_state`), the pre-match storyline (`get_storyline`), and the league history (`player_history`, `club_history`, `head_to_head`, `last_time`, rarity told in seasons); JSON with the meaning of every field; in process for the worker's agents, on stdio for any MCP host |
| The Writer through MCP | `src/worker/mcp_writer.py` | `--writer foundry --facts mcp`: the Writer reads `get_moment` from the server, with the same prompt, tool and JSON as today, and falls back to the local tool if MCP cannot open |
| Wren and Joss as two agents | `src/worker/duo_agents.py`, `src/worker/agent_desk.py` | `--duo agents`: after the card is on the desk with its template duo, two Agent Framework agents with their own MCP tools speak in a GroupChat whose running order is fixed by code; every turn passes the Verifier before the other agent hears it (one retry); the verified duo replaces the template one, otherwise the template stays and the audit says why |

Without these options nothing changes: same outputs byte for byte, and none of these modules is imported. They need the `agentic` extra (`pip install -c src/worker/constraints.txt -e ".[azure,agents,agentic]"`, the constraints pinning its packages too), which the worker image does not install: in the deployed demo, the Writer reads its facts through its local tool and the duo comes from the Writer or the templates.

## How to run it

Offline (no Azure, no cost), from the repository root, with the history and the storyline:

    python -m worker data/replay/demo-0007.jsonl.gz --writer template --duo agents --duo-dry --history data/history/9 --league-seed 9 --desk-dir data/desk

Under each card, the worker prints who wrote the duo (`duo agents:dry: wren, joss (player_history), wren, joss: 4 runs, 0.2 s`); `data/desk/demo-0007/cards.json` carries `duo_writer` and `duo_trace`, and `audit.jsonl` the `duo_written` and `duo_fallback` entries.

With the model (`az login`; `FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_MODEL` in `.env`; optional `FOUNDRY_DUO_MODEL` and `FOUNDRY_DUO_REASONING_EFFORT`, default `low`), one card first, with the template Writer so that the duo alone calls the model (6 runs at most):

    python -m worker data/replay/demo-0007.jsonl.gz --writer template --duo agents --duo-min-value 50 --duo-calls 6 --history data/history/9 --league-seed 9 --desk-dir data/desk

With `--writer foundry --facts mcp` in place of `--writer template`, the Writer and the recap agent also run on Foundry, for every card and the recap (and the insights and the storyline with `--insights-writer writer`), up to their own 40 runs per match: `--duo-min-value` and `--duo-calls` only bound the duo.

The MCP server for another host, over the folder the worker wrote:

    python -m worker.mcp_server --desk-dir data/desk --history data/history/9

## How to test it

    python -m pytest -q tests/test_mcp_server.py tests/test_mcp_writer.py tests/test_duo_agents.py tests/test_agent_desk.py
    python -m pytest -q

No test calls a model or the network: the chat clients are scripted (`tests/scripted_chat.py`) or dry. The tests cover each tool through an in-process `mcp.Client` and the stdio command (and a desk folder that reads nothing outside it), the Writer's tool served through MCP (the test fails if MCPStdioTool cannot connect), the running order, the Verifier at each turn and its retry (names at the start of a sentence, number words, feats, numbers in several words), a rejected line that the partner never hears, the ledger of tool facts and its conflicts, the budget and its reservations, the deadline and the cancellation, a failed hand-over, the producer's decisions kept, the demo match with the dry model, and the worker's command end to end with the dry model.

## Limits

The detail is in `docs/notes/agents.md` ("Not verified against the real API").

- Never run against the real Responses API since the port onto the current Writer: the partner's lines as user messages, the retry over a service-stored history, reasoning items and the three-language JSON are verified with scripted clients only.
- The fallback rate with a model is unknown, the Verifier being stricter for the agents, the first word of a sentence included. Proposed rule: more than half of the duos falling back, or duos over 30 s, and `--duo agents` stays off. When the agents fall back, an eligible card keeps the template duo, not the Writer's.
- Without `FOUNDRY_DUO_MODEL` on a deployment of its own, the duo shares the Writer's quota: check the tokens per minute first.
- The duo changes on the desk 10 to 25 s after the card, possibly after a producer approved it (audited; the texts never move). Whether it should is still to decide.
- A right number can still be used with a misleading meaning, a number from a text fact included; human review stays.
- The cap of 36 duo runs per match was set on the demo only.
- With `--duo agents` a replay is not deterministic: a card VAR rules out while its job waits ends `cancelled`, `retracted` or with the agents' duo, depending on the timing.
- The image deployed to Azure does not install the `agentic` extra: `--duo agents` and `--facts mcp` run locally only.
- In a replay, the end of the run waits for the queued duos (one at a time). Live with `--forever`, a duo that finishes after the last flush of a match waits for the next session.
