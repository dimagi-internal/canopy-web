# Converging ace-web's run execution onto canopy's harness

**Date:** 2026-07-26
**Status:** Draft for review
**Context:** follows the chat cutover (`2026-07-25-ace-web-canopy-chat-cutover-design.md`), which moved *chat* to canopy and left ace-web's *run* execution in place.

## Goal

ace-web stops being a second execution engine. Its programmatic ACE runs — `opps.api::seeded_run`, `resume_run`/`resume_interrupted`, the `drive_turn` command, Slack-triggered runs — enqueue a canopy `Turn` that canopy's headless cloud runner executes, instead of spawning `claude -p` inside ace-web's own ASGI worker.

## Why

ace-web runs a subprocess pool *inside the web process*. An ECS task replacement kills every in-flight run, the service is pinned to one instance because of it, and there is no ledger, no routing, no failover, and no cancel. canopy already solved all of that for chat: an append-only `TurnEvent` ledger, directed runner routing with failover and grace, real cancel, and readiness drills. Two engines is one too many, and the weaker one is the one holding load-bearing automation.

The execution models already match. canopy's **cloud** runner (`deploy/ec2-runner/cloud_runner.py`) is headless and "runs `claude -p` (stream-json) on the turn's prompt, streams the assistant/tool output into the `TurnEvent` ledger" — the same thing `turn_driver` does, with a lifecycle around it. (canopy's *other* runner drives the emdash GUI over CDP; that one is not a fit and is not what this uses.)

**Risk posture:** nothing depends on ace-web's run execution today — ACE work happens locally. That is what makes this migration safe to do now, and it will not be true later.

## Three findings that shape the design

### 1. Runs must target a session, not the agent

The obvious mapping — `Turn(agent=ace)` — is wrong. `one_executing_turn_per_agent` (`apps/harness/models.py`) is a unique constraint on the agent for claimed/running turns, so **every ACE run in the fleet would serialize to one at a time**. That is a severe regression from ace-web's concurrent runs.

The correct mapping is **one canopy `Session` per ace-web opp-run**, with `session.agent = ace`, and Turns targeting that **session**. Concurrency is then governed by `one_executing_turn_per_session`, which matches ace-web's real behavior (one turn at a time within a run, many runs at once). `Turn.target` already resolves `chat_session.agent.slug`, so ACE still displays as "ace" everywhere. `origin_ref` is a free JSON field and carries `{opp_slug, run_id, step_skill}` with no schema change.

### 2. The cloud runner cannot claim session turns at all

`claim_next_turn` gates session-targeted turns on `runner.session_capable()` (`capabilities.sessions == true`). The cloud runner never sets it — its own code says *"this runner does not declare capabilities.sessions today, so it is inert now"*. Wiring that in is a required, concrete piece of work, not a detail.

### 3. The ledger is lossier than ace-web's features need

ace-web's cost and structure views do not read a summary — they retain the **raw JSONL** (`IngestUpload.raw_jsonl_gz`) and re-derive from scratch on every read. They need, per line: `usage` (input/output/cache-creation/cache-read) and `model`; `uuid`/`parentUuid`/`isSidechain` for phase and sidechain nesting; each tool call's `id`, `name` and `input`; each result's `tool_use_id`, `is_error` and content; and the original event timestamps.

What the cloud runner actually posts today: `assistant` → `{"text"}`, `tool_start` → `{"name"}`, `tool_end` → `{}`, and the CLI's `result` line is read for its text and then **discarded**. `TurnEvent.ts` is server-receipt time, not the CLI's timestamp. Nothing retains raw lines.

So cost would be unavailable, structure would be unbuildable, and — separately — `Turn.session_id` is a synthetic `cloud-<uuid>` placeholder and `run_claude` never passes `--resume`, so cheap CLI-level continuation does not exist either.

## Decision: canopy retains the raw transcript, and the ledger stays a live stream

Two options were on the table: enrich `TurnEvent` payloads until the aggregators can be rebuilt against them, or have canopy retain the raw transcript per turn.

**We do both, with different jobs.** The ledger stays what it is — a *live* stream for the UI, deliberately reduced. Alongside it, canopy retains the **raw JSONL per turn**, gzipped, exactly as ace-web already does. Rationale:

- ace-web's cost and structure aggregators are already written and tested against raw JSONL. Rebuilding them against a reduced event shape is work with no upside and a fidelity ceiling.
- "Store the bytes, re-derive on demand" survives questions nobody has asked yet. Any reduction we design today loses whatever the next feature needs.
- A transcript is the natural durable artifact of a turn. Retaining it makes canopy a better substrate for *any* embedder, not a special case for ace.

The ledger is not made authoritative for cost; the transcript is. This keeps `TurnEvent` cheap and streaming-shaped and avoids a schema arms race.

We still enrich the events modestly — `tool_start`/`tool_end` gain a correlating `tool_use_id` — because without it a stream with parallel tool calls is genuinely ambiguous to render, which is a live-UI defect independent of this migration.

## Shape of the work

1. **canopy — raw transcript retention.** A gzipped raw-JSONL store keyed to the turn, written by the runner, readable by the turn's tenant. Cheap, additive, no change to existing consumers.
2. **canopy — cloud runner becomes session-capable**: declare `capabilities.sessions`, claim session turns, run in the right cwd, and capture the CLI's `system/init` session id (via `RunnerBinding.session_key`) so `--resume` works across turns.

   > **Amended 2026-07-26 after implementation (#426). Declaring the capability is NOT sufficient, and turning it on without the rest is a data-loss bug.**
   >
   > A session's durable record does not come from the `TurnEvent` ledger. `create_session` stamps `transcript_sourced` on every session at birth whenever `CHAT_STUB_EXECUTOR=False` — i.e. always, on labs — and `project_events` then returns 0 for it. The only durable path is `persist_transcript_rows`, fed by `POST /runners/{id}/session-stream`, which **only `packages/canopy_runner` implements**.
   >
   > So a runner that declares `capabilities.sessions` without that path becomes eligible to claim real chat turns whose record it can never write: the reply streams perfectly, and on reload the conversation is empty — including the user's own message. `/api/canopy-sessions/{id}/reset` cannot rebuild it either, because there is no backfill.
   >
   > #426 therefore ships the capability **opt-in and defaulted OFF** (`RUNNER_SESSIONS`). Flipping the default on requires first implementing the durable-record path for this runner. It now posts the raw transcript per turn, so deriving session `Message` rows server-side from that is the natural way to close it — but it is unbuilt work, not a config change.
3. **canopy — `tool_use_id` correlation** on `tool_start`/`tool_end`.
4. **ace-web — enqueue instead of spawn.** `seeded_run`, `resume_run`, `resume_interrupted`, `drive_turn` create/reuse a canopy Session per opp-run and enqueue a Turn. `turn_driver`'s subprocess path is retired only once all callers are migrated.
5. **ace-web — cost/structure read the retained transcript** from canopy instead of local `IngestUpload`.
6. **ace-web — an explicit "no runner available" state.** canopy already classifies a stuck queued turn as `config` (nothing declares this target) vs `offline` (a runner could take it but none are reachable) after a 150s grace, via `GET /turns/unclaimable`. Today an enqueued turn with no runner sits `QUEUED` silently and forever — nothing auto-fails it. ace-web must surface that distinction plainly on the run, rather than showing a run that looks like it is working.

## Out of scope

Standing up an always-on cloud runner fleet. The software is built to degrade visibly without one; provisioning EC2 is a separate cost/ops decision.

## Flagged, unresolved — RESOLVED 2026-07-26

The Slack run path (`apps/slack/run_starter.py`) creates a `Session` and a user `Message` but no call to `start_turn_subprocess` could be found in `apps/slack/**` — Slack-triggered runs may already be latent or broken independent of this migration. Confirm before migrating that caller.

> **Answer: it was never wired.** `/ace run <existing-slug>` is latent; `/ace new` and `/ace run <pdd-link>` are broken. `run_starter.py:126-144` creates the Session and one completed `role="user"` Message and returns — no assistant placeholder, no `turn_driver` import, and the repo has no signals, no Celery, no custom `Message.save()`, and a deleted WS consumer, so nothing executes it. The post-deploy sweep cannot rescue it either: `resumable_after_deploy`/`interrupted` both require an *assistant* row.
>
> `git log --follow` shows the birth commit already ending at `return slug, run_id` — the wiring never existed. The claim came from a design doc and was transcribed into the docstring. Every Slack test mocks `start_run_from_slack`, which is why CI stayed green since May.
>
> Consequence for this migration: **that caller is not migrated, it is repaired** — separately, and last. Separately again, `run_starter.py:93` passes `settings.ACE_DRIVE_SA_KEY_JSON` (a `str`) to `GoogleDriveClient`, which requires a credentials object.
