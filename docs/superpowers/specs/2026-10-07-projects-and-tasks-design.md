# Projects and Tasks — one clean model, one clear UI

**Date:** 2026-10-07 · **Status:** draft for review · **Owner:** Jonathan (decisions), Ada (spec)

## Why

canopy now has exactly one project-management system — `AgentProject` + `AgentTask`
(the workbench Projects registry was retired in #1212). But the system still speaks
in the vocabulary of everything it replaced, and a person new to canopy meets five
nouns for two things:

| What a newcomer sees | What it actually is |
|---|---|
| **Work** (nav) | the task board |
| **Inbox** (nav) | tasks with an open ask |
| **Items** (`/api/items/`, `list_items`, `decide_item`, `create_items`) | tasks with an ask — `Item` was folded into `AgentTask` (#873) and only the name survived |
| `list_waiting_tasks` | a second route for the same queue |
| **Work products** (nav) | a flat link table nothing writes to (12 rows fleet-wide, last write 2026-07-20) |

And projects — the top of the hierarchy — have no page of their own: they exist only
as group headers inside Work's "by project" view.

People are being added to the canopy ecosystem. The goal is a structure that is
**obvious to a person on first look and identical in shape to what an agent reads**.
Jonathan himself mostly works through sessions, not this UI; the bar is clarity, not
power-user features.

## The model

Two nouns. Nothing else.

| Noun | Model | Id | Meaning |
|---|---|---|---|
| **Project** | `AgentProject` | `P1`, `P2` … (per agent) | A piece of work with an end — "UNGA 2026 conference planning". |
| **Task** | `AgentTask` | `ext_id` (per agent) | One step, optionally inside a project. |

A task may **ask** a person something. That is a property of the task, not a third
noun: `ask_kind` is `review` (approve / skip / defer) or `question` (answer in words),
and the ask is **open** until it is answered or dismissed. "Waiting on you" means *a
task whose ask is open and whose `waiting_on_user` is you* — it is a filter, not a
place.

The word **item** is removed everywhere: routes, MCP tools, schemas, frontend
components, docs, agent skills. The word **work product** is removed likewise.

### Task fields: the ask gets one prefix

`AgentTask` carries Item's old field names "deliberately" (models.py comment) so the
dispatcher could read either. With Item gone that reason is gone, and the unprefixed
names (`decision`, `comment`, `decided_by` …) read as properties of the *task*. They
are properties of the *ask*. Rename:

| Today | After |
|---|---|
| `ask_kind`, `ask_body` | unchanged |
| `decision` | `ask_decision` |
| `comment` | `ask_comment` |
| `decided_by`, `decided_by_user`, `decided_at` | `ask_decided_by`, `ask_decided_by_user`, `ask_decided_at` |
| `ask_dismissed` | unchanged |
| `dispatch`, `dispatched_at` | `ask_dispatch`, `ask_dispatched_at` |

`uuid` is dropped as a public address: a task is addressed by `(agent slug, ext_id)`,
exactly like a project. (The column can stay if the dispatcher's idempotency keys use
it; it is no longer in any URL or response.)

### Deliverables

Deliverables matter to the **agent** — so it knows where a project stands when it picks
its next work — not as a human-facing gallery; things get shared by direct link. So:

- A project's deliverables live in its existing `links` field (`[{title, url}]`),
  written by the agent (`PATCH` the project, or `canopy agent project link P2 <url>`).
- `AgentWorkProduct` is retired: its routes, page, card, count and CLI verb
  (`agent-publish` work products) go. The 12 rows are stale and their files are in
  Drive; the table drop is a separate, later migration (data deletion waits; the
  feature removal does not).
- `Turn.work_product_urls` is unaffected (it is turn provenance, not this table).

## The UI

### Agent left nav

```
Projects     ← landing page
Tasks   (3)  ← badge = open asks waiting on you
Turns
Schedules
Huddles
Skills
Settings
```

*Work* is renamed **Tasks**. *Inbox* and *Work products* are removed as entries. Old
URLs redirect: `/work → /tasks`, `/inbox → /tasks?waiting=me`, `/items → /tasks`,
`/work-products → /projects`.

**Status reports** (manager-sync self-reviews, today a section of Work products) move
to a section on **Turns** — they are the agent's review of its own turns.

### Projects page — `/agents/:slug/projects`

One row per project, active first:

```
P2  Connect Enterprise                         Active   Jonathan   3 open · 1 waiting   2d ago
    A clear "what" explanation of Connect Enterprise … (outcome, one line)
```

Done and archived projects sit in a collapsed group below. "Add project" takes a name
and an outcome ("what done looks like") — outcome is required in the UI because a
project without one is a task.

### Project page — `/agents/:slug/projects/:ref`

Always the same four sections, in this order:

1. **Header** — ext_id, name, outcome, status (editable: active / done / archived),
   owner, Drive folder link, repo.
2. **Tasks** — this project's tasks, the same cards as the Tasks board, open first;
   tasks with an open ask show it inline and can be answered in place.
3. **Activity** — the recent turns that touched this project (a turn touches it when
   its `task_ext_ids` include one of the project's tasks), newest first, each linking
   to the turn.
4. **Links** — the agent-recorded deliverables, collapsed by default.

### Tasks page — `/agents/:slug/tasks`

Today's board, plus one filter bar:

```
[ Waiting on you · 3 ]  [ Open ]  [ Done ]      Project: [ All ▾ ]      Group by project ☐
```

- **Waiting on you** is what Inbox was: open asks, reviews before questions, oldest
  first, answerable in place. The ask card is the task card in its asking state —
  `ItemCard` folds into `TaskCard`, so there is one card.
- **Project** filters to one project or "No project".
- **Group by project** is today's `ByProject` view.

### Fleet-wide

The `/supervisor` inbox (fleet `ItemInbox`) becomes **Waiting on you** across agents —
the same filter, backed by the fleet tasks route below.

## The API (and therefore the MCP tools)

Every REST route is an MCP tool, so the API *is* the agent's view. End state:

```
GET    /api/agents/{slug}/projects/                     ?status=
POST   /api/agents/{slug}/projects/
GET    /api/agents/{slug}/projects/{ref}/               → project + tasks + recent turns + links
PATCH  /api/agents/{slug}/projects/{ref}/

GET    /api/agents/{slug}/tasks/                        ?project=P2|none &status= &waiting=me|any &ask=open|decided|dismissed &batch=
POST   /api/agents/{slug}/tasks/                        one task, or a batch (idempotency_key per task) — absorbs create_items
GET    /api/agents/{slug}/tasks/{ref}/
PATCH  /api/agents/{slug}/tasks/{ref}/
POST   /api/agents/{slug}/tasks/{ref}/answer            {decision?: implement|skip|defer, comment?} — absorbs decide_item
POST   /api/agents/{slug}/tasks/{ref}/dismiss           {comment?}                                 — absorbs dismiss_item
POST   /api/agents/{slug}/tasks/{ref}/commands          (unchanged)
POST   /api/agents/{slug}/tasks/sync                    (unchanged)

GET    /api/tasks/                                      fleet-wide, same filters + &agent=  — absorbs GET /api/items/
GET    /api/projects/                                   fleet-wide ?status= &repo_slug=     — absorbs GET /api/agent-runs/projects/
```

`{ref}` is the ext_id (`P2`, a task's ext_id), as projects already do. Task routes move
from the integer database id to `{ref}`.

**Removed** (no shims — a stale caller gets a 404 and is fixed, per the fleet's
retire-loudly rule):

- `GET/POST /api/agents/{slug}/items/`, `GET /api/items/`, `GET /api/items/{id}/`,
  `POST /api/items/{id}/decide`, `POST /api/items/{id}/dismiss`
- `GET /api/agents/{slug}/tasks/waiting/`
- `GET/POST /api/agents/{slug}/work-products/`
- `GET /api/agent-runs/projects/`

`get_project`'s response is the project page's four sections, so an agent asking "where
does P2 stand?" gets exactly what a person sees.

## Who calls what's being removed (all fixed in the same change)

| Repo | Caller | Becomes |
|---|---|---|
| canopy-web | `frontend/src/api/items.ts`, `ItemCard`, `ItemAge`, `ItemInbox`, `InboxSection`, `ItemsSection`, `AgentWorkProductsSection`, `WorkProductCard`, `TasksBoard`, `AgentLeftNav`, `SupervisorPage`, guide surfaces | task routes + one `TaskCard` |
| canopy-web | `scripts/e2e_embed_widget.py`, `docs/architecture/api-surface.md`, `frontend/e2e/*` | task routes |
| canopy (plugin) | `agent_web.push_items` (`agent-publish` items), `agent_web.push_work`, `agent_health` open-items read, `agent-core/turn.md`, any runner/DDD use of `GET /api/agent-runs/projects/` (no caller inside canopy-web) | `POST …/tasks/` batch, `GET …/tasks/?ask=open`, `GET /api/projects/` |
| ada | `bin/ada-publish`, `bin/ada-withdraw`, `bin/_canopy_http.py`, `config/gating.json` (dismiss rail), `skills/conduct`, `skills/fleet-review`, `skills/fleet-sync/templates/round1.md` | task routes; Ada's stored ids become `(slug, ext_id)` |
| eva | 2 references | task routes |
| ace, echo, hal, jarvis | none found by grep — **re-grep every agent repo before the removal PR** | — |

## Sequencing

The minimum that never leaves a caller with nothing to call:

1. **canopy-web PR 1 — add.** New task routes (`{ref}` addressing, filters, `answer`,
   `dismiss`, batch create), fleet `/api/tasks/` and `/api/projects/`, the field
   renames (one migration), `get_project` sections, and the whole new UI (nav, Projects
   page, project page, Tasks filter bar, one `TaskCard`, redirects, Status reports on
   Turns). The old item routes still exist, reading the renamed fields.
2. **Callers.** canopy plugin, ada, eva (and anything the re-grep finds) move to the
   task routes — one PR each.
3. **canopy-web PR 2 — remove.** Item routes, `tasks/waiting/`, work-product routes and
   UI, `/api/agent-runs/projects/`, the `apps/harness/items_api.py` module, the word
   "item" from docs and the guide.
4. **Later — data.** Drop the `AgentWorkProduct` table.

## Testing

- Backend: route tests for every filter combination that replaces a removed route
  (`waiting=me`, `ask=open|decided|dismissed`, `batch=`, `project=none`), `answer` for
  review vs question (a question is closed by its comment, not a decision), 409 on
  double-answer, and that `get_project` returns all four sections.
- A test that fails if any route path, schema name or MCP tool name contains `item`
  (outside unrelated words) — so the noun cannot creep back.
- Frontend: Projects page, project page sections, the Tasks filter bar (each filter
  maps to the right query), redirects from the four old URLs.
- After PR 2: each caller repo's tests pass against the new routes; `canopy agent
  doctor --all` is clean.

## Out of scope

- **Two verb sets on a task.** Board commands (`accept / decline / dispatch / reassign /
  edit / comment / done`) and ask answers (`implement / skip / defer`) overlap. Merging
  them is a real simplification but a separate change; this spec only moves the ask
  verbs onto task routes.
- Cross-agent (shared) projects — projects stay per agent (decided 2026-09-19).
- Any Drive scanning of project folders.
