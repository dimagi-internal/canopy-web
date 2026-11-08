# Agent tasks — call an agent inline, get a structured result

An **agent task** is a page (or any backend, or a script) calling a canopy agent
INLINE: it hands the agent a task name, structured input and the JSON Schema of the
answer it needs, and gets back a result that validates against that schema. It is not
a chat panel ([embedding-a-canopy-agent.md](embedding-a-canopy-agent.md) is that) and
its output is not a transcript: the only output is the result.

A dynamic view can start one it declares, as its viewer — [dynamic-views.md](dynamic-views.md) § The one write.

Code: `apps/agent_tasks` (framework tier, L1 core — see `ARCHITECTURE.md`); start at
its `models.py` docstring, then `services.py` (what happens) and `api.py` (who may call).
Routes, bodies and size limits are `api.py` / `schemas.py` and the OpenAPI schema
(`create_agent_task`, `get_agent_task`, `submit_agent_task_result`,
`cancel_agent_task`); the limits are the `MAX_*` constants in `services.py`. Client:
`canopy_sdk.agent_tasks.AgentTasks` (`sdk/python/README.md` § Agent tasks). Not
`agents.AgentTask`, which is a board task ([work.md](work.md)).

## Rules

- **Creating is exactly an agent turn's gate.** `api.create_agent_task` runs
  `turn_gates.check_shape`, `resolve_target`, `check_asker` and `clis.refusal`, so a
  route here can never be wider than `POST /api/harness/turns/`. The task and ONE turn
  are written in one transaction (`services.enqueue_turn` with `origin: api`,
  `idempotency_key: agent-task:<id>`, `origin_ref: {"agent_task": "<id>"}`).
- **A reader who may not read it gets a 404, never a 403**, so a task's existence does
  not leak (`api._task_or_404`). `input` is returned only to the agent's own login
  (`api._out`).
- **Only the agent's own login submits a result**: its PAT, or the login canopy issues
  a runner for one turn (`turn_agent_tokens`). Anyone else who can read the task gets
  a 403. A confined turn (a caller's read mode or host site) reaches canopy as its
  asker, not as the agent, so it cannot submit: agent tasks are for turns that run as
  the agent.
- **A refused result keeps the task open.** The result is validated against
  `output_schema` (`services.result_errors`); a failure is a 422 whose `errors` lists
  `{path, message}` so the agent fixes each path and submits again.
- **The turn's prompt is fixed and short** (`services.prompt`), and the task itself is
  never pasted into it: the input can be a megabyte, and a prompt is a transcript row.
  It tells the agent the task is read-and-answer only, with no outbound action.
- **`origin_ref.agent_task` is the server's to stamp**: `POST /api/harness/turns/`
  drops it (and `agent_task_nudge`) from a client's `origin_ref`, like `acts_as`
  (`harness/turn_gates.py`).
- Tenancy derives from the agent (`agent.workspace`), as an agent turn's does, so a
  moved agent takes its tasks.

## The completion rule

**A task is done only when the agent submits a result that validates against the
schema — never because a turn ended.** Agents end turns early (they "respond too
soon"), so a turn's end is never read as an answer. Two rails hold the rule:

1. **The server backstop** (`apps/agent_tasks/signals.py`, `services.on_turn_ended`).
   Every terminal transition of a turn writes a `status` row on its ledger, and the
   ledger fires `turn_events_appended` after commit. When the turn that ended is the
   task's LATEST turn and the task is still open: a CANCELLED turn (someone stopped
   it) fails the task; any other end sends a follow-up turn (`services._nudge`) — a
   fresh agent turn with `origin_ref: {"agent_task": id, "agent_task_nudge": n}`,
   `parent` the ended turn, canopy as its initiator accountable to the task's creator
   — up to `MAX_NUDGES`; the next end fails the task. A follow-up is not a send into
   the ended turn's session: that is a person's chat send path (its ACL, sealing and
   authorship), and a nudge has no person behind it. On a laptop runner a turn
   finishes as soon as its session exists while the agent works on in it, so nothing
   is sent while that session is still live on the runner
   (`turn_agent_tokens.live_session`); the deadline still holds.
2. **The canopy plugin's Stop hook** (the canopy repo). The caller envelope names the
   task a turn holds — `agent_task: {id, name, status}`, null otherwise
   (`harness.caller_context.build`; data, never prompt) — and the hook refuses to let
   the agent end its turn while that task's status is not `done`.

**The deadline.** An open task past `deadline_at` (created + `timeout_s`) fails: on any
read of it, and from the off-request job `agent_tasks.expire` (`apps/agent_tasks/apps.py`,
`apps/common/ticks.py`). A task that ends while its turn is still queued cancels that
turn.
