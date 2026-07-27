# Runner convergence and live observability

**Date:** 2026-07-27
**Status:** Draft for review
**Supersedes in part:** the live-chat-bridge half of `2026-07-22-reusable-chat-kit-design.md`
**Related:** `2026-07-24-persist-runner-session-transcript-design.md`, `2026-07-26-run-execution-convergence-design.md`

## Problem

The laptop runner and the cloud runner have diverged into two implementations of
the same job, and the split is not where anyone intended it.

The intended difference is **process supervision only**: locally emdash wraps the
session so a human can jump into it; in the cloud `claude -p` runs headless (and
could be attached to over tmux). Everything downstream — the conversation, the
tool calls, the record — is the same data.

The actual difference, measured 2026-07-27:

| | laptop | cloud |
|---|---|---|
| claim / heartbeat / start / finish / events | ✅ | ✅ |
| `/turns/{id}/transcript` | ✅ | ✅ |
| `/runners/{id}/sessions` | ✅ | ❌ |
| `/streams` + `/session-stream` | ✅ | ❌ |
| `/backfills` + `/session-backfill` | ✅ | ❌ |

The cloud runner does not participate in the session/message layer at all.
Because `services.transcript_sourced()` returns true only for
`origin == ORIGIN_RUNNER`, a cloud-executed session is **ledger-sourced**: its
durable rows are projected from reduced `TurnEvent`s and keyed by a dense
counter, while a laptop session's rows come from the transcript keyed by
ordinal. Same conversation, two records, two fidelities, two keying schemes.

There are also two independent implementations of the same block extraction
(`chat_bridge._rows_for_record` vs. the inline loop in `run_claude`) and two
byte-chunkers for the same endpoint.

The clearest evidence that the shared core is real: both codebases have
independently written the *same function*. `cloud_runner._encode_project_dir`
and `canopy_transcript`'s path encoding are byte-identical
(`str(cwd).replace("/", "-").replace(".", "-")`).

## What we were building on

canopy's live view is built entirely on tailing `~/.claude/projects/**/*.jsonl`.
That file is **not a documented interface**. It appears in the Claude Code docs
only as `transcript_path` on hook payloads, carrying this warning:

> The transcript file is written asynchronously and may lag the in-memory
> conversation, so it may not yet include the current turn's most recent
> messages when a hook fires. Hooks that need the final assistant text of the
> current turn should use `last_assistant_message` on Stop and SubagentStop
> instead of reading the transcript.

So we depend on an internal, and we depend on it for *liveness* — the one
property the docs say it does not have. Meanwhile three first-class surfaces
went unused:

- **Hooks** (25 events). `PreToolUse` / `PostToolUse` / `PostToolUseFailure` /
  `PostToolBatch` push `tool_name`, `tool_input`, `tool_use_id` and results in
  real time, on a documented schema, with no lag. `MessageDisplay` fires as
  assistant text streams. Available to **both** emdash-driven and `claude -p`
  sessions.
- **`--output-format stream-json --include-partial-messages`** — token-level
  `text_delta`. Cloud path only; emdash owns the interactive process.
- **OpenTelemetry** — usage/cost metrics and events.

## The split

The two surfaces have genuinely different properties, and the product has
genuinely two modes:

| Mode | What the user needs | Surface |
|---|---|---|
| Watching or typing | The last few messages, immediately. Deciding whether to break in. | Live, lossy, arrival-ordered |
| Clicking between parallel sessions | Complete, ordered history | Durable, complete, lags |

This is both/and, not either/or, and the two map onto the two surfaces exactly.

### The load-bearing invariant

**The transcript is the reconciler, so the hook path is allowed to fail.**

A hook that cannot POST — canopy-web deploying, runner restarting, network
blip — costs a few seconds of live latency and nothing else, because the
transcript backfills the same content into the durable store regardless.

That invariant is what makes both/and cheap instead of twice the work, and it
dictates the hook implementation: **fire-and-forget, hard local timeout, no
queue, no retry, no durability**. It is also a safety requirement, not just an
optimisation — `PreToolUse` can *block* a tool call, so observability must never
be able to stall an agent.

## Architecture

```
                    ┌─ LIVE (lossy, ~instant) ─────────────────┐
Claude Code ──hooks─┤ tool_use_id, tool_input, tool_result      │──┐
 (emdash or -p)     └───────────────────────────────────────────┘  │
      │                                                             ├─► canopy-web
      └─ writes ──► transcript .jsonl ──┐                           │
                                        └─ DURABLE (complete, lags) ┘
                                           ordinal-keyed Message rows
```

One durable store (`Message`, ordinal-keyed — unchanged). One live overlay that
never persists.

## Shared core: `packages/canopy_transcript`

Django-free, no emdash/CDP dependencies, installable. Follows the `canopy_cron`
precedent — that package exists because the server and runner had two copies of
slot math, which is the same failure this fixes.

```
packages/canopy_transcript/
  paths.py      emdash convention | cwd + cli_session_id convention
  tail.py       TailReader (byte offset, last_raw)
  rows.py       block → row, BLOCK_STRIDE, caps, NUL scrub
  batching.py   chunk_raw_lines
  hooks.py      hook payload → the same row shape
```

Consumers: `canopy_runner` (laptop), `cloud_runner.py` (cloud), and
`apps/canopy_sessions` for `BLOCK_STRIDE` — which the server currently
*documents in a comment* rather than importing.

**Packaging constraint:** `cloud_runner.py` is deliberately a single file,
delivered gzip+base64 via Secrets Manager because EC2 UserData caps at 16 KB.
The box already clones canopy-web to `/opt/canopy-web`, so the package is
installed from that clone during cloud-init rather than vendored.

## Live path

**Producer is hooks on both runners.** This is the convergence point: one live
producer, identical payloads, whatever started the session.

The cloud runner's stdout `stream-json` stays as an **additive** enrichment
only — `--include-partial-messages` gives token deltas the emdash path cannot
get, because emdash owns the process. Same baseline everywhere; richer where the
process allows.

### Transport: through the runner, over localhost

```
hook (fire-and-forget, hard timeout)
  └─► POST localhost:PORT/hook
        runner: claude session_id + cwd → canopy Session
        └─► POST /session-stream   (runner identity, existing auth)
```

The identity mapping is why. A hook knows Claude's `session_id` and `cwd`;
canopy knows the `Session` and `RunnerBinding`. The runner is already the thing
that maps between them — resolving it anywhere else duplicates that knowledge.
It also keeps auth out of the hook (the runner already holds a PAT) and lets the
runner coalesce or drop under load.

### Hook installation

**Once per machine, at user level** (`~/.claude/settings.json`), not per
session — so it covers every emdash worktree and every `claude -p` on that box
automatically, with no per-session setup. The runner filters incoming events by
`cwd` down to sessions it has bindings for; everything else is dropped cheaply.

### Trap: `--bare`

`--bare` **skips hook auto-discovery**, and the docs state it "will become the
default for `-p` in a future release." The cloud runner must therefore pass
hooks explicitly via `--settings` rather than relying on discovery, or it will
silently go dark on a future Claude Code upgrade. This is a latent,
time-delayed failure, so it belongs in the code as a comment, not just here.

## Durable path

Unchanged in shape from `2026-07-24-persist-runner-session-transcript-design.md`:
the transcript is tailed, rows are keyed by the composite ordinal
(`record * BLOCK_STRIDE + block`), and `persist_transcript_rows` is the single
write path. What changes is that **both** runners now feed it through the shared
core, so a cloud session becomes transcript-sourced like a laptop session.

`claude -p` writes a normal transcript at
`~/.claude/projects/<encoded-cwd>/<session_id>.jsonl` — the cloud runner already
reads that path in `_resume_target_exists`, so no new mechanism is needed, only
a second consumer of an existing file.

## Reconciliation

**Client-side.** The tail is a view concern and must never enter the
ordinal-keyed store — provisional rows in `Message` would defeat the ordinal
scheme that makes stream/backfill/reset idempotent.

```
render = sorted(durable, key=turn_index) ++ tail(arrival order)
```

| | key | lifetime |
|---|---|---|
| durable row | `turn_index` (composite ordinal) | forever |
| live tail entry | `tool_use_id`, else `(prompt_id, arrival index)` | until its durable row lands |

- A durable row arriving **evicts** its tail entry by `tool_use_id`. That key is
  present on both surfaces (hooks pass it directly; the transcript carries it as
  `tool_use.id` / `tool_result.tool_use_id`) and is the same key
  `pairToolMessages` already pairs on.
- **Assistant text has no shared key** — `MessageDisplay` carries no message
  uuid and the transcript's record `uuid` is not exposed to hooks. Text is
  therefore *replace-on-arrival*: the durable row supersedes the tail entry
  wholesale. Text is cheap to re-render and the only visible artifact is a brief
  re-render, which is why this is the accepted trade rather than inventing a
  synthetic key.

**Attach** re-snapshots durable history and opens a fresh tail. **Detach** stops
consuming; the transcript keeps filling the archive unwatched. There is no gap
handling, because the archive is authoritative and complete either way.

## What this supersedes

- The live chat bridge's prose-only `TurnEvent` path. Hooks replace it, and the
  double-render hazard it was designed around (the same records reaching the
  client twice under two message ids) disappears with the single live producer.
- The cloud runner's inline block extraction in `run_claude`.
- `capabilities`-style divergence in which endpoints each runner speaks.

## Out of scope

- **OpenTelemetry.** Deferred, and explicitly *not* a replacement for retained
  transcripts. The fleet runs on Claude **subscriptions**, so there is no
  per-token billing and `claude_code.cost.usage` is a modelled number (tokens ×
  list price), not real spend — true of any source, including transcript-derived
  figures. What is real is **tokens**, which the transcript carries per record
  (`usage`: input / output / cache-creation / cache-read). Retaining the raw
  transcript therefore remains the right basis for usage accounting.
- **Token-level deltas** (`--include-partial-messages`). Cloud-only and additive;
  build after the common baseline works.
- **tmux into a runner session as an alternate UI.** Genuinely complementary —
  raw terminal fidelity and interactivity versus a structured, searchable,
  multiplayer stream — but a different product surface that should not block
  this.

## Spike results (2026-07-27) — the live path is confirmed

Run against this machine, from inside an emdash-driven session.

**1. Hooks fire in an emdash session, and hot-reload.** A `PostToolUse` hook
installed at user level fired on the *very next* tool call with no restart of
the session or of emdash. The assumption the whole live path rested on holds.

**2. emdash already uses this exact transport.** Its worktrees carry a
`.claude/settings.local.json` whose hooks POST to
`http://127.0.0.1:$EMDASH_HOOK_PORT/hook` with `-d @-` (forwarding the hook JSON
verbatim), a per-session `$EMDASH_HOOK_NONCE`, `curl -sf`, and `|| true`. That
is the design in this spec, already running in production on the same machines.
It **resolves the localhost-listener question**: the precedent exists, loopback
+ per-session nonce is the established auth, and the fire-and-forget idiom is
proven.

**3. emdash uses only `UserPromptSubmit`, `Notification`, `Stop`** — no tool
events. canopy's hooks **compose** with emdash's rather than colliding. canopy
must install at **user level** (`~/.claude/settings.json`, whose `hooks` is
currently empty) and leave the emdash-managed project file alone, since emdash
owns and rewrites it.

**4. One user-level install covers every concurrent session.** The probe
captured events from *two different sessions* at once — this one, and an
unrelated agent running in `…/emdash/cloud-7s5ii` — distinguishable by
`session_id` and `cwd`. The "install once per machine, filter by cwd" design is
empirically confirmed rather than assumed.

**5. `PostToolUse` carries the result, not just the input.** Payload keys:

```
cwd, duration_ms, effort, hook_event_name, permission_mode, prompt_id,
session_id, tool_input, tool_name, tool_response, tool_use_id, transcript_path
```

`tool_response` is a dict (`stdout`, `stderr`, `interrupted`, `isImage`,
`noOutputExpected` for Bash). **One event is therefore a complete tool_use +
tool_result pair**, where the transcript splits them across two records. The
live tail can emit an already-paired row and skip the correlation step
entirely — pairing by `tool_use_id` remains necessary only for reconciling
against durable rows.

**6. Payloads are small and carry timing.** ~783 bytes for a Bash call, and
`duration_ms` gives real per-tool latency — a signal the transcript does not
contain at all.

### Still unverified

- **`MessageDisplay` granularity** — per chunk or per message. Determines
  whether streamed text is genuinely incremental on the laptop path. Does not
  gate the tool path, which is the valuable half.
- **Hook overhead on a tool-heavy turn**, measured rather than assumed. The
  floor cost looked negligible (the probe's own hook added no perceptible
  latency), but it should be measured against a turn with many rapid tool calls
  before shipping to the fleet.

## Sequencing

This is more than one implementation plan. It decomposes into four slices, each
shippable and each leaving the system working:

0. ~~**Spike: confirm hooks fire in an emdash session.**~~ **Done, passed** —
   see Spike results above. The live path is unblocked.
1. **Extract `packages/canopy_transcript`** and repoint the laptop runner at it.
   Pure refactor, no behaviour change, no new surface. Independently valuable —
   it removes the duplicate chunker and block extractor even if the rest stalls.
2. **Cloud runner becomes transcript-sourced**: depends on the shared core,
   tails its own `claude -p` transcript, and speaks `/session-stream`,
   `/backfills`, `/runners/{id}/sessions`. At the end of this slice a cloud
   session and a laptop session produce the *same* durable record — which is the
   convergence goal, independent of the live path.
3. **Live path**: hook install, runner listener, client tail + reconciliation.
   The only slice that adds new surface area.

Slices 1 and 2 are worth doing on their own merits — at the end of slice 2 a
cloud session and a laptop session produce the same durable record, which is the
convergence goal independent of the live path.

## Open questions

- Should the runner drop hook events for sessions with no attached viewer
  (`stream_desired = False`)? It would cut chatter substantially, at the cost of
  a colder first paint on attach. Leaning yes, with the tail seeded from durable
  rows on attach — but this is tunable after measurement, not a design fork.
  Note the spike measured payloads at ~783 bytes, so the raw volume argument is
  weak; the real cost is the POST per tool call, not the bytes.
- Does the cloud runner keep stdout parsing at all once hooks land, or is
  `--include-partial-messages` the only reason to read stdout? Resolve when
  token deltas are built.
