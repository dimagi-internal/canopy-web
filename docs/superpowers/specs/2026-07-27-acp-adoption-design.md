# ACP adoption: the live layer becomes a standard

**Date:** 2026-07-27
**Status:** Spiked and confirmed — see Spike results
**Related:** `2026-07-27-runner-convergence-and-live-observability-design.md` (this
resolves that spec's live-path open questions and supersedes its hand-rolled wire shape)

## Problem

canopy hand-rolled a live agent-observation protocol: hook payloads translated
into bespoke `chat.tool_use` frames, `toolPreview()` heuristics guessing a
human-readable label out of tool inputs, `chat.stream_start`/`delta`/`complete`
for assistant text, and usage derived from the transcript.

**Agent Client Protocol (ACP)** already specifies all of it, as an open standard
modelled on LSP. emdash does not scrape Claude — it runs
`@agentclientprotocol/claude-agent-acp`, which wraps
`@anthropic-ai/claude-agent-sdk`. Both packages are public npm. The protocol we
were inventing exists, is supported, and covers Codex agents too.

| What canopy built | What ACP specifies |
|---|---|
| hook payload → `tool_use`/`tool_result` rows | `tool_call` + `tool_call_update` |
| reconcile on `tool_use_id` | `toolCallId` |
| live row vs durable row | `status`: pending → in_progress → completed |
| `chat.stream_*` | `agent_message_chunk` (grouped by message) |
| usage derived from the transcript | `usage_update` |
| `toolPreview()` guessing labels | `kind` + `title`, authored by the agent |
| (nothing) | `agent_thought_chunk`, `plan`, `session_info_update`, rate limits |

## Spike results (2026-07-27) — all green

Run on this laptop against a live Claude **subscription**, driving
`@agentclientprotocol/claude-agent-acp` 0.63.0 over raw JSON-RPC/stdio.
Scratch client: `spike.mjs` / `spike2.mjs`.

**1. Subscription auth holds.** `initialize` returns `authMethods: []` — no auth
step at all; the adapter uses the same ambient credentials the CLI does. The
refusal path the static read had found (`--hide-claude-auth`) is not on by
default. Positive confirmation, not just absence of refusal: `usage_update`
carries `_meta["_claude/rateLimit"] = {status: "allowed", resetsAt: …,
rateLimitType: "five_hour", overageStatus: "rejected"}` — the five-hour window
*is* the subscription limiter. This was the single most likely thing to sink
adoption. It didn't.

**2. An ACP session writes a normal transcript.** `session/new` returns a
`sessionId` that is Claude's own session id, and the session writes
`~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl` exactly like any other.
**The durable path is therefore untouched by ACP** — ordinals, reset, backfill,
`persist_transcript_rows` all keep working with no changes. This decouples the
two halves of the convergence work entirely (see Sequencing).

**3. `toolCallId` is the transcript's `tool_use.id`** (`toolu_01…`). The
reconciliation key the convergence spec needed for live→durable eviction is
native to both surfaces. No synthetic keys, no correlation step.

**4. Tool results arrive on the live stream.**
`_meta.claudeCode.toolResponse` carries the same `{stdout, stderr, interrupted,
isImage, noOutputExpected}` dict `PostToolUse` hooks carry, plus `rawOutput` and
a rendered `content` block. The existing hook→row mapping transfers nearly
verbatim.

**5. `session/load` replays the whole conversation** as `user_message_chunk` /
`agent_message_chunk` updates, preserves context across processes, and **appends
to the same transcript file**. That is `--resume` with a free backfill.

**6. Headless is safe.** Available modes: `auto` (a classifier decides),
`default`, `acceptEdits`, `plan`, `dontAsk`, `bypassPermissions`. Under the
default `auto` a `Write` completed with **no** permission request — a headless
cloud turn will not silently block on a prompt.

**7. Latency is fine.** 16 updates in 5.7s for a one-tool turn, streaming text
at sub-word granularity.

## What this changes versus the prior plan

The prior agreement was: spike on the cloud runner → make the internal protocol
ACP-shaped → client renders ACP → ask emdash to expose its stream. That holds.
Three refinements come out of the spike:

**(a) Sequencing splits cleanly, because the durable path is untouched.** The
convergence spec bundled "cloud runner becomes transcript-sourced" with ACP.
Finding 2 shows these are independent: cloud transcript-sourcing needs no ACP,
and ACP disturbs no ordinal. Ship them separately; neither can break the other.

**(b) Rate-limit metadata is a first-class product signal, not a curiosity.**
canopy's operating loop is *shift the fleet to the next account when this one
runs out of tokens*, and today `RunnerAssignment`'s cascade is purely reactive —
it fails over once a runner is offline, wedged, or 60s late. `_claude/rateLimit`
makes that **predictive**: a runner can report `status`, `resetsAt` and
`rateLimitType` on its heartbeat, so the cascade can step down *before* a turn
dies rather than after. Nothing else in the system carries this. It should ride
the heartbeat onto `Runner` and surface on the Runners tab.

**(c) `session_info_update` supplies the title.** PRs #475/#476/#477 were all
title-derivation repair. On the ACP path the agent supplies it.

One caution to carry into the client work: `tool_call_update` is a **sparse
patch** stream — intermediate updates omit `status`, `title`, `kind` entirely
(observed: 4 updates for one Bash call, only the last carrying `status:
completed`). The client must merge patches keyed by `toolCallId` rather than
treating each update as a complete row. Rendering a patch as a row is the most
likely way to ship a visibly broken transcript.

## What ACP does not do

ACP is a **live** protocol. It has no durable, ordinal-keyed archive, no
multi-viewer fan-out, and no cross-device history. The transcript work,
composite ordinals, reset/backfill and the `Message` store all stay exactly as
they are. ACP replaces the live layer and the wire shape, not the record.

It is also editor↔agent shaped: one client driving one agent. canopy is a server
observing many agents for many viewers on a phone. The remote HTTP/WebSocket
transport exists but is less proven than stdio; the runner stays the ACP client
and canopy-web stays the fan-out, which is the boundary we already have.

## emdash stays. Web chat is a second view of the same pattern

**emdash remains the local execution surface, and keeping local work in it is a
requirement, not a default.** It is the most efficient way to interact with a
session on this machine, and nothing here trades that away.

What changed is the *mental model*, not the plan: emdash is no longer the only
jump-in surface. The multiplayer chat on canopy-web (and ace-web) is one too, so
emdash-session and web-chat-session are **two views of the same pattern** rather
than two different kinds of thing. The consequence is that they should speak one
protocol and offer the same interactions — not that either replaces the other.

That raises the bar for the web view rather than lowering it for emdash: for a
session running in emdash, the web should be able to do what emdash can do —
steer it mid-turn, interrupt it, answer a permission prompt, see its slash
commands. Today the web's send/stop reach an emdash session through CDP
injection, which is why permissions in particular have no web equivalent at all.

ACP is the protocol that describes all of it. Spiked 2026-07-27, confirmed on a
live session:

- **Steering** — `initialize` reports `_meta.steering.supported: true` and
  `promptQueueing: true`. A second `session/prompt` sent while the first is
  still running lands mid-turn: the running prompt ended early and the agent
  obeyed the interjection. That is exactly typing into emdash while it works.
- **Interrupt** — `session/cancel` mid-tool-call returned
  `stopReason: "cancelled"`. That is Escape.

Two more come free and have no equivalent on the CDP path at all:
`session/request_permission` (the web can render approve/deny, which CDP
injection cannot do), and `available_commands_update` (the session's slash
commands, delivered as data — observed in the spike carrying the full skill
catalog). Plus `session/set_mode` for plan / acceptEdits / bypassPermissions.

So the split stays where the convergence spec put it — **process supervision
only** — and ACP is adopted where there is no emdash to supervise:

| | laptop | cloud |
|---|---|---|
| supervisor | **emdash** (unchanged) | none — headless |
| producer | hooks + transcript, translated to ACP | ACP, native |
| jump in via | the emdash window, **and** the web chat | the web chat |
| wire shape | ACP | ACP |

The laptop does **not** grow a canopy-driven ACP executor. A canopy-spawned ACP
session would be a session emdash knows nothing about, which is the one outcome
worth avoiding: it would cost the jump-in that makes the laptop worth running.
Nothing in the CDP path is removed — the injector, the collision heuristics and
native dialog, the emdash-DB reads and the `verify-emdash` guard all stay,
because they are what make local execution land in a window you can take over.

What ACP earns locally is the **shape**, not the executor. The hook/transcript
output translates into `tool_call` / `tool_call_update` / `agent_message_chunk`
so the client renders one protocol whatever produced it, and the web view of an
emdash session can grow toward emdash's own interactivity instead of inventing a
parallel vocabulary for it.

If emdash ever exposes its own ACP stream (`acpApiContract` /
`acpAgentStatusBridge` are internal IPC today), the translator is deleted and a
real ACP client swaps in — still emdash-owned, still jump-in-able, **with no
client-side change at all**, because the client was already speaking the
protocol. That is the best end state for the laptop, and it is an ask of emdash
rather than something to route around.

## Sequencing

1. **`canopy_acp`** — a Django-free shared package: spawn/attach the adapter,
   JSON-RPC framing, the client-side handlers (`fs/*`, permissions), and the
   sparse-patch merge. Follows the `canopy_cron` / `canopy_transcript`
   precedent. Used by **both** runners, which is what makes 2 and 3 one job.
2. **Cloud runner executes turns over ACP** instead of `claude -p` +
   stream-json. Deletes the stdout parser. `session/load` replaces `--resume`.
3. **ACP becomes the internal wire shape.** The laptop's hook/transcript output
   translates into `tool_call` / `tool_call_update` / `agent_message_chunk`;
   the client merges patches by `toolCallId` and renders ACP only. emdash keeps
   executing local work throughout — this changes the wire, not the executor.
4. **Rate-limit telemetry onto the heartbeat**, and into the cascade.
5. **Ask emdash to expose its ACP stream.** Then delete the translator and the
   laptop is native ACP *without* giving up emdash — the best end state.

Steps 1–2 are independently valuable and carry no client risk. Step 3 is the
one that touches rendering.

Improving the **web view of an emdash session** (answering a permission prompt,
steering, interrupting) is worth doing on its own merits and is tracked
separately — it is a parity goal for the second view, not part of adopting ACP
in the cloud.
