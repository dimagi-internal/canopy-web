# Dynamic views

A **dynamic view** is a screen whose code is data: JSX defining `function View(props)`, usually
written by an agent, saved on a workspace or one of its projects, and drawn in a sandbox over
canopy's own data. It is how someone gets a screen canopy does not have — "this project's open
work by assignee", "which agents failed this week and on what" — without a deploy.

Code as data, a sandboxed frame with no session, and data only through reads the page makes for
it as the viewer.

## Where to start reading

| Piece | Where |
|---|---|
| Model, append-only revisions | `apps/dynamic_views/models.py` (docstring), `services.py` |
| The catalog: every read a view may ask for | `apps/dynamic_views/catalog.py` (docstring) |
| Declared agent tasks | `apps/dynamic_views/declared_tasks.py` |
| Declared connections | `apps/connections/declarations.py` |
| Routes (each an MCP tool) | `apps/dynamic_views/api.py` |
| The contract served to an author: render contract, kit, example | `apps/dynamic_views/guide.py`, `GET /api/dynamic-views/guide/` (`get_dynamic_view_guide`) |
| The frame (document, CSP) | `apps/dynamic_views/frame.py`; its runtime `frontend/src/dynamicViews/frame/` |
| The canopy page's side (bridge) | `frontend/src/dynamicViews/bridge.ts`, `agentTasks.ts`, `connectionCalls.ts`, `pagePanel.ts`, `DynamicViewFrame.tsx`; message types in `protocol.ts` |

## How a view is drawn

The page frames `/dynamic-views/frame/` with `sandbox="allow-scripts"` and **no
`allow-same-origin`**. The frame posts `ready`; the page reads the catalog, reads each declared
source through the bridge, and posts `render` with the code, the data and the `scope`. The frame
compiles the code (sucrase), runs it with React, its hooks and the kit passed in as parameters, and
draws it inside an error boundary. On-demand reads, refresh, navigation and agent tasks are
messages to the page; the page answers or acts.

The page accepts a message only when `event.source` is its own frame's window; the frame accepts
one only from `window.parent` at an origin the server named in the shell (`window.CANOPY_DV.origins`).

## What keeps it safe

**The frame can do nothing on its own.** Opaque origin: no canopy cookies, storage or DOM.
Its CSP (`frame.py::csp`) sets `sandbox allow-scripts` so the document is opaque even opened
directly, `connect-src 'none'` (no fetch, XHR, WebSocket, EventSource), and `frame-ancestors`
canopy's own origins. `DynamicViewFrameMiddleware` answers before sessions and auth, so the
response never carries a `Set-Cookie`.

**Every read is a catalog read, as the viewer, in the view's workspace.** `resolveRead`
(`bridge.ts`) refuses an unknown model, a parameter the model does not list, a non-scalar value, a
path parameter that is not a plain identifier, or any attempt to name the workspace — `{workspace}`
is always the view's own. The page then GETs `/api/w/<workspace>/<path>` with the viewer's
session, so the route's own gate decides what comes back. A view can show nothing its viewer could
not already read. `services.create`/`update` check declared sources against the same catalog, so a
bad source is refused when the view is written, not on every draw.

**The one write: an agent task the view declares.** Nothing else about a view writes. Its code may
call `canopy.runAgentTask(...)`, and the page starts that agent task
([agent-tasks.md](agent-tasks.md)) — but only for an `{agent, name}` pair in the view's
`agent_tasks`:

- **Checked when written** (`declared_tasks.validate`, from `services.create`/`update`): each agent
  is one of the view's workspace's agents, and the person saving it may give that agent a task
  themselves (`declared_tasks.may_dispatch`: `agent.work` in its workspace and
  `turn_context.resolve_turn_context` not refusing them — the same two gates `POST
  /api/agent-tasks/` runs). New code on a view that declares tasks is checked against its writer
  the same way, so a project lead who is only a workspace viewer can keep a view's title but
  cannot rewrite code that calls an agent. A restore restores the declaration with the code.
- **Refused before it is sent** (`checkAgentTask`, `agentTasks.ts`): an undeclared pair, or a
  field a view has no business setting (`parent`), never reaches the server.
- **Run as the viewer.** The page POSTs `/api/w/<the view's workspace>/agent-tasks/` with the
  viewer's own session, so the server's gate decides: a workspace editor, admin or owner the
  agent takes work from may run a declared task; a workspace viewer, a project lead who is only a
  viewer, a contact or a non-member may not. The task is the viewer's: they are its creator and the
  turn's initiator, and only they and the agent's admins may read or cancel it.
- **Bounded to the view.** `canopy.agentTask(id)` reads only a task this frame started. Leaving
  the page — or the frame navigating itself away — cancels every task the view is still waiting on
  (`ViewAgentTasks.cancelAll`).

**What is left, said plainly.** Data handed to the frame can leave it — a WebRTC connection, or
the code navigating its own frame elsewhere (the page stops answering a frame that reloads, but the
navigation itself is not blocked). So a view's author can see what the view shows its viewers. What
the sandbox removes is the author acting AS the viewer, or reaching anything the view does not show.
That is why the catalog leaves out anything secret (vaults, tokens, credentials, mail bodies,
transcripts): put a read in it only if showing it to everyone who can open the view is fine.
`test_the_catalog_reads_nothing_secret` holds the list to that.

**Who may change a view** (`api._require_edit`). Reading a view needs membership of its workspace.
Creating, changing, restoring or deleting one needs `agent.work` (editor) in its workspace, or edit
rights on its project (`agents.gates.may_edit_project`). Moving a view needs those rights where it
lands as well, so a project lead cannot put a view on the workspace. Every save, and every restore,
is a new version, so history only grows.

## The chat panel beside a view

On a view's own page, the view can do what any site embedding canopy's SDK does
(`canopy.page.setState`, `registerAction`, `ask`). The frame cannot reach the panel itself (opaque
origin, no network), so it asks over the protocol and `pagePanel.ts` registers on its behalf in
canopy's page registries (`widget/pageState.ts`, `widget/pageActions.ts`). An agent's call comes
back to the frame as `page-action`, runs in the view as the viewer, and times out
(`ACTION_TIMEOUT_MS`); everything is withdrawn when the view leaves the screen. Not on a project
page, which draws several views and has one page state.

An action can do only what the view itself can, so the agent gains nothing the person could not do
on that screen. A view keeps decisions the person must make, such as sending or applying changes, as
buttons and does not offer them as actions.

canopy's own panel lists, on a page in workspace W, W's agents (`DelegatedToken.own_panel` /
`page_workspace`, `apps/embedding/delegation.py::offered_agent_ids`).

## Declared connections (Salesforce calls)

A view may call Salesforce through canopy-web only as it declared: `connections` on the view
(snapshotted on every revision, restored with the code), validated on save by
`apps/connections/declarations.py` (its limits and rules are there). Code calls
`canopy.connection.request(...)`; the page refuses anything undeclared
(`connectionCalls.ts`) and the server checks again against the revision the page drew. Writes need
a click and an operation lease, bounded by the screen's declaration; `as: "shared"` needs the
agent's admin to have approved the project's declared lines. A rejection carries `e.connect_url`
when connecting would fix it, which the frame can only reach through `canopy.navigate`. Readers see
the declaration beside the declared tasks (`DeclaredConnections.tsx`). The trade and its limits:
[`vault-connections.md`](vault-connections.md) § Calls from screens.

## Adding to the catalog

One `Model(...)` line in `catalog.py`: a name, the REST read's path relative to `/api/w/<ws>`, a
description, and the query parameters a view may pass. The frontend reads the catalog from the guide
route, so nothing else changes. Before adding one, ask whether everyone who can open a view should
see what it returns — the author will.
