# Projects and Tasks — one clean model, one clear UI

**Date:** 2026-10-07 · **Status:** approved direction, revised for one-step cutover · **Owner:** Jonathan (decisions), Ada (spec)

## Why

canopy now has exactly one project-management system — `AgentProject` + `AgentTask`
(the workbench Projects registry was retired in #1212). But it still speaks the
vocabulary of everything it replaced, and a person new to canopy meets five nouns
for two things and two verb sets for one:

| What a newcomer sees | What it actually is |
|---|---|
| **Work** (nav) | the task board |
| **Inbox** (nav) | tasks with an open ask |
| **Items** (`/api/items/`, `list_items`, `decide_item`, `create_items`) | tasks with an ask — `Item` was folded into `AgentTask` (#873); only the name survived |
| `list_waiting_tasks` | a second route for the same queue |
| **Work products** (nav) | a flat link table nothing writes to (12 rows fleet-wide, last write 2026-07-20) |
| board **commands** `accept / decline / dispatch / reassign / edit / comment / done` *and* ask **decisions** `implement / skip / defer` + `dismiss` | ten verbs, mostly synonyms, for "a person acted on a task" |

Projects — the top of the hierarchy — have no page of their own.

People are being added to canopy. The goal is a structure that is **obvious to a
person on first look and identical in shape to what an agent reads**. Jonathan works
mostly through sessions; the bar is clarity, not power-user features.

**Cutover rule (Jonathan, 2026-10-07):** no backwards compatibility. Existing task data
is mutated straight to the final shape (or dropped where it has no place in it); old
routes, tools and fields are deleted, not aliased; every caller is fixed right after.

## The model

Two nouns.

| Noun | Model | Id | Meaning |
|---|---|---|---|
| **Project** | `AgentProject` | `P1`, `P2` … per agent | A piece of work with an end — "UNGA 2026 conference planning". |
| **Task** | `AgentTask` | `T1`, `T2` … per agent | One step, optionally inside a project. |

A task may **ask** a person something — a property of the task, not a third noun.
`ask_kind` is `review` ("should I do this?") or `question` ("I need an answer"), and
the ask is **open** until an action closes it. **Waiting on you** = live tasks with an
open ask, or parked on you via `waiting_on_user`. It is a filter, not a place.

Removed everywhere — routes, MCP tools, schemas, components, docs, agent skills: the
words **item**, **work product**, **command**, **decision**.

### One action set

Everything a person does *to* a task is one of five **actions**, through one route.
Field changes (title, owner, who it's waiting on, project …) are a plain `PATCH`, not an
action.

| Action | Does | Closes an open ask | Agent follow-up | Who may |
|---|---|---|---|---|
| `approve` | status → in progress; runs the task's `on_approve` turns if it has any | yes | pending unless `on_approve` ran | viewer |
| `decline` | status → declined; comment kept as the reason | yes | none | viewer |
| `reply` | adds a comment. On a **question** it is the answer: closes the ask and runs `on_approve` if set | on a question | pending unless `on_approve` ran | viewer |
| `dispatch` | queue the agent to work this task now | no | pending | editor |
| `done` | status → done | yes | none | editor |

What it replaces: `accept`+`implement` → **approve**; `decline`+`skip`+`dismiss` →
**decline**; `comment` + answering a question → **reply**; `dispatch` → **dispatch**;
`done` → **done**; `edit`/`reassign` → **PATCH**; `defer` → removed (not acting *is*
deferring; the ask stays open).

Every action is recorded as an **`AgentTaskAction`** row (renamed from
`AgentTaskCommand`): `task`, `action`, `comment`, `by` (user + display string),
`created_at`, `status` (`pending` / `applied`), `applied_at`, `result_note`. That row is
both the task's history and the agent's to-do queue: the agent drains
`status=pending` and marks each applied. "Who approved this and why" is the closing
action row — so the task needs no decision fields of its own.

### Task fields — final shape

Kept: `agent`, `ext_id`, `project`, `run`, `title`, `next_action`, `status`
(`suggested / in_progress / done / declined`), `owner`, `assigned`, `waiting_on_user`,
`confidence`, `score`, `review`, `rationale`, `source_url`, `plan`, `due`, `links`,
`notes`, `position`, `source`, `batch_key`, `idempotency_key`, `origin`, `origin_ref`,
`raised_by`, `created_at`, `updated_at`.

The ask: `ask_kind`, `ask_body`, `ask_closed_at` (null = open).

Renamed: `dispatch` → `on_approve` (the turn specs that run when it is approved or its
question answered); `dispatched_at` stays.

Dropped: `uuid`, `decision`, `comment`, `decided_by`, `decided_by_user`, `decided_at`
(→ `ask_closed_at`), `ask_dismissed`. A task is addressed by `(agent slug, ext_id)`,
the same way a project is.

### Deliverables

They matter to the **agent** (so it knows where a project stands), not as a human
gallery — things are shared by direct link. A project's deliverables live in its
existing `links` field (`[{title, url}]`), written by the agent. `AgentWorkProduct` —
model, table, routes, page, card, count, CLI verb — is deleted outright.
`Turn.work_product_urls` is unaffected (turn provenance, not this table).

## The UI

### Agent left nav

```
Projects     ← landing page
Tasks   (3)  ← badge = waiting on you
Turns
Schedules
Huddles
Skills
Settings
```

*Work* → **Tasks**. *Inbox*, *Items* and *Work products* are gone. Old paths redirect:
`/work → /tasks`, `/inbox → /tasks?waiting=me`, `/items → /tasks`,
`/work-products → /projects`. **Status reports** (manager-sync self-reviews) move to a
section on **Turns**.

### Projects page — `/agents/:slug/projects`

One row per project, active first:

```
P2  Connect Enterprise                         Active   Jonathan   3 open · 1 waiting   2d ago
    A clear "what" explanation of Connect Enterprise … (outcome, one line)
```

Done and archived collapse into a group below. "Add project" takes a name and an
outcome (required in the UI: a project without "what done looks like" is a task).

### Project page — `/agents/:slug/projects/:ref`

Always these four sections, in order:

1. **Header** — ext_id, name, outcome, status (editable: active / done / archived),
   owner, Drive folder, repo.
2. **Tasks** — this project's tasks, open first, same card as the Tasks board.
3. **Activity** — recent turns that touched it (a turn touches it when its
   `task_ext_ids` include one of the project's tasks), newest first, linking to the turn.
4. **Links** — the agent-recorded deliverables, collapsed by default.

### Tasks page — `/agents/:slug/tasks`

Today's board plus one filter bar:

```
[ Waiting on you · 3 ]  [ Open ]  [ Done ]      Project: [ All ▾ ]      Group by project ☐
```

**One card** (`TaskCard`; `ItemCard` is deleted). A card shows its ask when open and
offers exactly the actions that apply: an open review → *Approve · Decline*; an open
question → a reply box (+ *Decline*); any live task → *Reply*, and for editors
*Dispatch · Done*. Its history is the task's action rows.

### Fleet-wide

`/supervisor`'s inbox becomes **Waiting on you** across agents — the same filter and
card, backed by `GET /api/tasks/?waiting=me`.

## The API (and therefore the MCP tools)

Every REST route is an MCP tool, so this list *is* the agent's view.

```
GET    /api/agents/{slug}/projects/                 ?status=
POST   /api/agents/{slug}/projects/
GET    /api/agents/{slug}/projects/{ref}/           → project + tasks + recent turns + links
PATCH  /api/agents/{slug}/projects/{ref}/

GET    /api/agents/{slug}/tasks/                    ?project=P2|none &status= &waiting=me &ask=open|closed &batch=
POST   /api/agents/{slug}/tasks/                    a LIST of tasks (one or many); idempotency_key per task replays
GET    /api/agents/{slug}/tasks/{ref}/              → task + its actions
PATCH  /api/agents/{slug}/tasks/{ref}/
POST   /api/agents/{slug}/tasks/{ref}/actions       {action, comment?}

GET    /api/agents/{slug}/actions/                  ?status=pending      ← the agent's queue
POST   /api/agents/{slug}/actions/{id}/applied      {result_note?}

GET    /api/tasks/                                  fleet-wide, same filters + &agent=
GET    /api/projects/                               fleet-wide ?status= &repo_slug=
```

`POST /tasks/sync` (the legacy sheet upsert) is deleted — `POST /tasks/` with
idempotency keys does its job.

**Deleted, no aliases:** `/api/agents/{slug}/items/`, `/api/items/…` (list, get,
decide, dismiss), `/tasks/waiting/`, `/tasks/sync`, `/tasks/{id}/commands`,
`/commands`, `/commands/{id}/apply`, `/work-products/`, `/api/agent-runs/projects/`,
and `apps/harness/items_api.py`.

## Data migration (one migration, final shape)

- `AgentTask`: `ask_closed_at = decided_at`; drop `uuid`, `decision`, `comment`,
  `decided_by`, `decided_by_user`, `decided_at`, `ask_dismissed`; rename `dispatch` →
  `on_approve`. A decided ask's old comment is not carried (accepted loss).
- `AgentTaskCommand` → `AgentTaskAction`: map `accept→approve`, `comment→reply`,
  `decline/dispatch/done` unchanged; **delete** `edit` and `reassign` rows (they were
  field edits). Field `kind` → `action`, `payload` → `comment` (the payload's
  `note`/`reason` text, else empty), `created_by` → `by`.
- Drop `AgentWorkProduct`.

## Who calls what's deleted — fixed right after the canopy-web merge

| Repo | Caller | Becomes |
|---|---|---|
| canopy-web | `api/items.ts`, `ItemCard`, `ItemAge`, `ItemInbox`, `InboxSection`, `ItemsSection`, `AgentWorkProductsSection`, `WorkProductCard`, `TasksBoard`, `AgentLeftNav`, `SupervisorPage`, guide surfaces, `scripts/e2e_embed_widget.py`, `docs/architecture/api-surface.md`, `frontend/e2e/*` | task routes, one `TaskCard` (same PR) |
| canopy (plugin) | `agent_web.push_items`, `agent_web.push_work`, `agent_health` open-items read, `canopy agent tasks` / commands drain, `agent-core/turn.md` + `task-tracker.md`, any use of `/api/agent-runs/projects/` | `POST …/tasks/`, `GET …/tasks/?ask=open`, `GET/POST …/actions/`, `GET /api/projects/` |
| ada | `bin/ada-publish`, `bin/ada-withdraw`, `bin/_canopy_http.py`, `config/gating.json` (dismiss rail → decline rail), `skills/conduct`, `skills/fleet-review`, `skills/fleet-sync/templates/round1.md` | task routes + `decline` action |
| eva | 2 references | task routes |
| ace, echo, hal, jarvis | **grep every agent repo for `items`, `commands`, `work-products`, `tasks/sync`, `tasks/waiting` before merging canopy-web** | — |

## Sequencing

1. **canopy-web, one PR:** migration, new routes, deletions, whole UI.
2. **Immediately after:** canopy plugin, then ada and eva (and anything the grep found),
   one PR each. Between 1 and 2 the agents' board calls fail loudly — accepted.

## Testing

- Backend: each `tasks/` filter (`waiting=me`, `ask=open|closed`, `batch=`,
  `project=none`); each action's state change and agent follow-up from the table above;
  `reply` on a question closes it, on a review doesn't; 409 on acting on a closed ask
  with `approve`/`decline`; permission split (viewer vs editor actions); batch create
  replays on `idempotency_key`; `get_project` returns all four sections; the migration
  maps a decided ask and each command kind correctly.
- A guard test that fails if any route path, schema or MCP tool name contains `item`,
  `work_product`, `command` or `decision`.
- Frontend: Projects page, project page sections, the Tasks filter bar (each filter →
  right query), the card's action set per state, redirects from the four old paths.
- After step 2: each caller repo's tests pass; `canopy agent doctor --all` is clean.

## Out of scope

- Cross-agent (shared) projects — projects stay per agent (decided 2026-09-19).
- Any Drive scanning of project folders.
