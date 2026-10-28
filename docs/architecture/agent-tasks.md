# Agent tasks — call an agent inline, get a structured result

An **agent task** is a page (or any backend, or a script) calling a canopy agent
INLINE: it hands the agent a task name, structured input and the JSON Schema of the
answer it needs, and gets back a result that validates against that schema. It is not
a chat panel ([embedding-a-canopy-agent.md](embedding-a-canopy-agent.md) is that) and
its output is not a transcript: the only output is the result.

A dynamic view can start one it declares, as its viewer — [dynamic-views.md](dynamic-views.md) § The one write.

Code: `apps/agent_tasks` (framework tier, L1 core — see `ARCHITECTURE.md`). Client:
`canopy_sdk.agent_tasks.AgentTasks` (`sdk/python/README.md` § Agent tasks).

## The contract

| Route | MCP tool (operationId) | Who | What |
|---|---|---|---|
| `POST /api/agent-tasks/` | `create_agent_task` | whoever may enqueue a turn for the agent (`agent.work`) | `{agent, name, instructions?, input, output_schema, routing?, timeout_s?, parent?}` → 201, the task, `status: queued` |
| `GET /api/agent-tasks/{task_id}?wait_s=0..20` | `get_agent_task` | its creator, the agent's admins, the agent's own login | the task; `wait_s` long-polls until its status changes |
| `POST /api/agent-tasks/{task_id}/result` | `submit_agent_task_result` | the agent's own login, while the task is open | `{result}` → the task, `status: done`; 422 with `errors` if it does not validate |
| `POST /api/agent-tasks/{task_id}/cancel` | `cancel_agent_task` | its creator, the agent's admins | an open task → `cancelled` (idempotent) |

Each also answers under `/api/w/{ws}/agent-tasks/…`. Anyone else gets a 404, never a
403, so a task's existence does not leak.

**Create.** The gates are exactly `POST /api/harness/turns/`'s for an agent turn
(`turn_gates.check_shape`, `resolve_target`, `check_asker`, `clis.refusal`): a viewer
is 403, a non-member 404. `output_schema` must be a JSON Schema object that
`jsonschema` accepts (`check_schema`; 422 naming the problem); `input` is any JSON, at
most 1 MiB serialized (413), `output_schema` at most 64 KiB (413), `instructions` at
most 20 000 characters. `timeout_s` is 30–3600, default 900. `routing` takes
enqueue_turn's values (default `prefer_local`). The task and ONE turn are written in
one transaction: `services.enqueue_turn` with `origin: api`, `idempotency_key:
agent-task:<id>`, `origin_ref: {"agent_task": "<id>"}` and the caller as its initiator.
A turn the agent's rules refuse outright (written CANCELLED at enqueue) fails the task
at once with the turn's reason.

**The turn's prompt** is a fixed, short template (`services.prompt`): read the task
with `get_agent_task`, do it — with the agent's skill when `name` names one (whole, or
before its first dot: `gmail-triage.review` → `gmail-triage`) — and finish ONLY by
calling `submit_agent_task_result`. It says the task is read-and-answer only: no
outbound action. The task itself is never pasted into the prompt (the input can be a
megabyte, and a prompt is a transcript row).

**Read.** `status` (`queued` → `running` → `done` | `failed` | `cancelled`), `result`
once done, `error` once failed or cancelled, `nudges`, `turn_id` (the latest turn
working it), `deadline_at`, `instructions` and `output_schema`. `input` is returned
only to the agent's own login. The agent's own read is how it takes the task: a queued
task becomes `running`.

**Result.** Only the agent's own login: its PAT, or the login canopy issues a runner
for one turn (`turn_agent_tokens`, a `PersonalToken` of `Agent.user`) — anyone else
who can read the task gets a 403. The result is validated against `output_schema`
(with format checking); a result that fails is a 422 problem whose `errors` member is
a list of `{path, message}`, `path` a JSON Pointer into the result (`""` for the
whole of it), at most 50 — and the task stays open, so the agent fixes each path and
submits again. A task that has ended is a 409; a result over 1 MiB a 413. A confined
turn (a caller's capability) reaches canopy as its asker, not as the agent, so it
cannot submit: agent tasks are for turns that run as the agent.

## The completion rule

**A task is done only when the agent submits a result that validates against the
schema — never because a turn ended.** Agents end turns early (they "respond too
soon"), so a turn's end is never read as an answer. Two rails hold the rule:

1. **The server backstop** (`apps/agent_tasks/signals.py`, `services.on_turn_ended`).
   Every terminal transition of a turn writes a `status` row on its ledger, and the
   ledger fires `turn_events_appended` after commit. When the turn that ended is the
   task's LATEST turn and the task is still open: a CANCELLED turn (someone stopped
   it) fails the task; any other end sends a follow-up turn — a fresh agent turn with
   `origin_ref: {"agent_task": id, "agent_task_nudge": n}`, `parent` the ended turn,
   canopy as its initiator accountable to the task's creator, and the prompt "You
   ended without submitting the result for agent task <id>. Call
   submit_agent_task_result now." followed by the task's own instructions — up to
   **2** follow-ups; the next end fails the task with `error: "the agent ended without
   a result"`. A follow-up is not a send into the ended turn's session: that is a
   person's chat door (its ACL, sealing and authorship), and a nudge has no person
   behind it. On a laptop runner a turn finishes as soon as its session exists while
   the agent works on in it, so nothing is sent while that session is still live on
   the runner (`turn_agent_tokens.live_session`); the deadline still holds.
2. **The canopy plugin's Stop hook** (the canopy repo; shipped there on its own). The caller envelope names
   the task a turn holds — `agent_task: {id, name, status}`, null otherwise
   (`harness.caller_context.build`; data, never prompt) — and the hook refuses to let
   the agent end its turn while that task's status is not `done`.

**The deadline.** An open task past `deadline_at` (created + `timeout_s`) fails with
`error: "timed out after <n>s without a result"`: on any read of it, and from a job
on the off-request clock (`apps/common/ticks.py`, `agent_tasks.expire`, every 60 s).
A task that ends while its turn is still queued cancels that turn.

`origin_ref.agent_task` is the server's to stamp: `POST /api/harness/turns/` drops it
(and `agent_task_nudge`) from a client's `origin_ref`, like `acts_as`.

## Storage

One `AgentTask` row (`apps/agent_tasks/models.py`): id (uuid), agent, name,
instructions, input, output_schema, status, result, error, nudges, turn, routing,
created_by, timeout_s, deadline_at, timestamps. Tenancy derives from the agent
(`agent.workspace`), as an agent turn's does, so a moved agent takes its tasks. Not
`agents.AgentTask`, which is a board task.
