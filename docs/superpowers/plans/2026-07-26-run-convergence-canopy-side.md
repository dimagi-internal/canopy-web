# Run convergence — canopy side (PRs 1–3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Make canopy's harness able to execute an ACE run as well as it executes a chat — retained raw transcript, a session-capable cloud runner with real `--resume` continuity, and unambiguous tool correlation. After this, ace-web can enqueue instead of spawn (a separate plan).

**Spec:** `docs/superpowers/specs/2026-07-26-run-execution-convergence-design.md`. Read it first — it carries the three findings that shape this work, and the decision that the ledger stays a reduced live stream while the **raw transcript** becomes the durable artifact cost/structure derive from.

**Architecture:** These are additive canopy capabilities that stand on their own — a retained transcript, a runner that can take session work, and correlated tool events all improve canopy for any consumer, not just ace-web.

## Global Constraints

- `uv run pytest`; `uv run ruff check . --select F --ignore F403,F405`; regenerate `frontend/src/api/generated.ts` and commit it when schemas/routes change (CI fails on stale types).
- Framework/product boundary holds: `apps/harness` and `apps/canopy_sessions` are framework — they must not import product apps.
- 404-not-403 for tenant resources.
- Commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- One branch per PR, off `origin/main`.

---

## PR 1 — Retained raw transcript (`feat/turn-transcript`)

The durable artifact. A turn's raw `claude -p` JSONL, gzipped, stored once and re-derivable on demand — the thing cost and structure aggregators actually need.

### Task 1: The store

**Files:** `apps/harness/models.py` (+migration), `apps/harness/services.py`, tests.

**Interfaces:**
```python
# a new model, sibling to TurnEvent — NOT a column on Turn (blobs don't belong on a hot row)
class TurnTranscript(models.Model):
    turn = models.OneToOneField(Turn, on_delete=models.CASCADE, related_name="transcript")
    raw_jsonl_gz = models.BinaryField()      # gzipped raw lines, exactly as the CLI emitted them
    line_count = models.PositiveIntegerField(default=0)
    bytes_raw = models.PositiveIntegerField(default=0)   # uncompressed size, for cost/ops visibility
    created_at / updated_at

services.append_transcript(turn, raw_lines: list[str]) -> TurnTranscript   # append-and-recompress, idempotent per turn
services.read_transcript(turn) -> bytes                                    # decompressed raw JSONL
```

- [ ] **Step 1 — failing tests:** appending lines stores them retrievably; appending twice **accumulates** rather than replacing (a turn streams in batches); `line_count`/`bytes_raw` reflect the accumulation; reading a turn with no transcript returns empty rather than raising; the blob round-trips exact bytes (no re-encoding, no line reordering).
- [ ] **Step 2** — run, confirm failures.
- [ ] **Step 3** — implement. Store gzipped; keep the model free of any parsing (canopy must not learn the CLI's schema — that is the consumer's business).
- [ ] **Step 4** — migration; full suite; commit.

### Task 2: Ingest + read routes

**Files:** `apps/harness/api.py`, `apps/harness/schemas.py`, tests.

**Interfaces:**
- `POST /api/harness/turns/{turn_id}/transcript` — body `{"lines": ["<raw jsonl>", ...]}`; runner-authenticated, appends. Bounded per request (reject absurd payloads with 422).
- `GET /api/harness/turns/{turn_id}/transcript` — returns the raw JSONL bytes (`application/x-ndjson`), tenant-gated by the **same** `_turn_or_404` used everywhere else.

- [ ] **Step 1 — failing tests:** a runner can append and read back; **a non-member of the turn's tenant gets 404 on both** (this is a transcript — treat leakage as seriously as the session-turn tenancy fix did); unknown turn 404s; an oversized batch 422s; appending to a terminal turn still works (the runner may flush after finishing).
- [ ] **Step 2** — run, confirm failures.
- [ ] **Step 3** — implement, reusing `_turn_or_404` rather than writing a parallel gate.
- [ ] **Step 4** — regenerate types; full suite; commit; open the PR.

---

## PR 2 — Cloud runner: session-capable, transcript-posting, resumable (`feat/cloud-runner-sessions`)

**Files:** `deploy/ec2-runner/cloud_runner.py`, `deploy/ec2-runner/README.md`, tests where the file's structure allows (it is stdlib-only and self-contained by design — do not add dependencies).

Read the file first. It currently claims agent/project turns, runs `claude -p --output-format stream-json`, reduces events to `assistant`/`tool_start`/`tool_end`, and finishes the turn. Its own docstring notes it is inert for sessions.

- [ ] **Step 1 — declare session capability.** `RUNNER_CAPS` gains `sessions: true` (env-gated, e.g. `RUNNER_SESSIONS=1`, default on for this runner kind). Verify against `claim_next_turn`'s gate (`runner.session_capable()`), and that `_turn_cwd` picks a sane working directory for a session turn — its docstring already anticipates this.
- [ ] **Step 2 — post the raw transcript.** Every raw stream-json line the CLI emits is forwarded verbatim to `POST /turns/{id}/transcript`, batched (do not hold the whole run in memory; flush periodically and on finish). This is *in addition to* the reduced TurnEvents, which keep streaming for the live UI. If a transcript POST fails, log and continue — a transcript failure must never fail the turn.
- [ ] **Step 3 — real `--resume` continuity.** Capture the CLI's `system/init` session id and report it via the existing `record_session`/`Session.cli_session_id` path (the model slot exists and is documented as unwired). On a later turn for the same session, pass `--resume <id>`; if resume yields nothing, fall back to a fresh spawn exactly as `packages/canopy_runner` does. Replace the synthetic `cloud-<uuid>` placeholder.
- [ ] **Step 4 — verify** end-to-end against a local canopy (`CHAT_STUB_EXECUTOR=False`) or, if that is impractical, state plainly in the report what was and was not exercised. Do not claim runtime verification you did not do.
- [ ] **Step 5** — update the README's capability/lifecycle notes; commit; open the PR.

---

## PR 3 — Correlated tool events (`feat/tool-use-id`)

Independent of the migration: with parallel tool calls, a flat `tool_start`×N / `tool_end`×N stream cannot be matched up, so the live UI can render tool results against the wrong call.

**Files:** `deploy/ec2-runner/cloud_runner.py`, `packages/canopy_runner/canopy_runner/chat_bridge.py` (if it emits tool events), `apps/canopy_sessions/stream_map.py`, the chat kit's tool pairing (`frontend/packages/canopy-ui/src/chat/pairToolMessages.ts`), tests.

- [ ] **Step 1 — failing tests:** `tool_start` carries `tool_use_id` and `input`; `tool_end` carries the same `tool_use_id`, `is_error`, and content; `stream_map` forwards the id to the client frames; the kit's pairing matches on id rather than order, and a **parallel** tool sequence pairs correctly (the case that is broken today).
- [ ] **Step 2** — run, confirm failures.
- [ ] **Step 3** — implement. Keep it backward-compatible: an event without an id must still render (older runners exist in the fleet), falling back to today's ordering heuristic.
- [ ] **Step 4** — full suite + `npm run test`/`npm run build`; regenerate types if schemas moved; commit; open the PR.

---

## Self-review notes

- The spec's decision is honored: the ledger stays reduced and streaming-shaped; the transcript is the durable, re-derivable artifact. Nothing here makes `TurnEvent` authoritative for cost.
- Tenancy is the one place to be paranoid — a transcript is more sensitive than a turn's status. PR 1 Task 2 reuses `_turn_or_404` deliberately rather than inventing a second gate, which is exactly how the earlier session-turn leak happened.
- Out of scope: standing up an always-on cloud runner (a cost/ops decision), and ace-web's caller migration (its own plan, which depends on all three of these).
