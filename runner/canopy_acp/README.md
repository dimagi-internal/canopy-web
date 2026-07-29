# canopy_acp

Django-free [Agent Client Protocol](https://github.com/agentclientprotocol) (ACP)
client for canopy's runners.

ACP is the open standard canopy had been hand-rolling. emdash does not scrape
Claude — it runs `@agentclientprotocol/claude-agent-acp`, which wraps
`@anthropic-ai/claude-agent-sdk`. The protocol already specifies the tool-call
lifecycle (`tool_call` / `tool_call_update` with a status), streamed reply text
(`agent_message_chunk`), thinking (`agent_thought_chunk`), the todo list
(`plan`), tokens (`usage_update`), and the session title
(`session_info_update`).

## What's here

| module | job |
|---|---|
| `updates.py` | `session/update` → canopy's row shape, via a **sparse-patch reducer** |
| `client.py` | JSON-RPC/stdio transport to the adapter: prompt, cancel, steer, load |

## The one thing to know

**`tool_call_update` is a patch, not a row.** Measured against
claude-agent-acp 0.63.0, a single `echo` produced five messages: the opener
carried a placeholder title (`"Terminal"`) and `status: pending`; the real title
arrived on the next; the output arrived on a message carrying *nothing else*;
and `status: completed` came only on the last.

So an absent field means **unchanged**, never **cleared**. Rendering any single
update as a row ships half-empty tool calls; taking a later update wholesale
erases the title. That is what `UpdateReducer` exists to prevent, and
`tests/test_updates.py` replays that exact recorded sequence.

## Rows are the existing contract

Rows produced here are interchangeable with
`canopy_transcript.rows_for_hook`'s, so the client renders one protocol whatever
produced it. They carry `index = -1` — live rows are a view overlay and are
never persisted. The durable record stays the transcript, which an ACP session
writes normally at `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`
(verified 2026-07-27), so adopting ACP costs the durable path nothing.

## Where it runs

The **cloud** runner, which has no emdash to supervise it. The laptop keeps
emdash as its executor — a canopy-spawned ACP session would be invisible to
emdash, which would cost the jump-in that makes the laptop worth running. The
laptop gains only the ACP *wire shape*, by translating its hook/transcript
output.

Design: `docs/superpowers/specs/2026-07-27-acp-adoption-design.md`.

## Tests

```bash
uv run --with pytest pytest -q
```
