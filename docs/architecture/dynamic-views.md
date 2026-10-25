# Dynamic views

A **dynamic view** is a screen whose code is data: JSX defining `function View(props)`, usually
written by an agent, saved on a workspace or one of its projects, and drawn in a sandbox over
canopy's own data. It is how someone gets a screen canopy does not have — "this project's open
work by assignee", "which agents failed this week and on what" — without a deploy.

The model is connect-labs' workflow and page engine (`connect_labs/workflow/page_mode.py`,
`components/workflow/sandbox/`, spec `docs/superpowers/specs/2026-10-12-page-sandbox-design.md`
in that repo): code as data, a sandboxed frame with no session, and data only through reads the
page makes for it as the viewer.

## The pieces

| Piece | Where | What it does |
|---|---|---|
| `DynamicView`, `DynamicViewRevision` | `apps/dynamic_views/models.py` | The view (workspace NOT NULL, optional project, slug unique per workspace, `code`, `sources`, `version`) and every saved version, append-only. |
| The catalog | `apps/dynamic_views/catalog.py` | Every read a view may ask for: a name, an existing REST read path, its allowed query parameters. The whole of what a view can reach. |
| Routes | `apps/dynamic_views/api.py` | `/api/dynamic-views/` list/create, `/{id}` get/patch/delete, `/{id}/revisions/`, `/{id}/rollback`, `/guide/`. Every one is an MCP tool. |
| The guide | `apps/dynamic_views/guide.py` | The render contract, the kit and a working example, served with the catalog by `GET /api/dynamic-views/guide/` (`get_dynamic_view_guide`). |
| The frame | `apps/dynamic_views/frame.py`, `frontend/src/dynamicViews/frame/` | `GET /dynamic-views/frame/`: the document the code runs in, and its runtime (compile, kit, protocol). |
| The bridge | `frontend/src/dynamicViews/bridge.ts`, `DynamicViewFrame.tsx` | The canopy page's side: frames the sandbox, turns its requests into catalog reads, answers them. |
| Pages | `/w/:ws/views`, `/w/:ws/views/:id`, the Views section of `/w/:ws/projects/:handle` | The list, one view (with the editor and history), a project's views drawn on its page. |

## How a view is drawn

1. The page frames `/dynamic-views/frame/` with `sandbox="allow-scripts"` and **no
   `allow-same-origin`**. The response's own CSP starts `sandbox allow-scripts`, so the document
   is opaque even opened directly.
2. The frame posts `ready`. The page reads the catalog (`/guide/`), reads each declared source
   through the bridge, and posts `render` with the code, the data and the `scope`.
3. The frame compiles the code with sucrase (JSX and TypeScript to `React.createElement`), runs it
   with `new Function` — React, its hooks and the kit passed in as parameters — and draws it
   inside an error boundary.
4. On-demand reads (`canopy.read`), `canopy.refresh()` and `canopy.navigate()` are messages to the
   page; the page answers or acts. The frame reports its height on every DOM change and resize.

The protocol is `frontend/src/dynamicViews/protocol.ts`. The page accepts a message only when
`event.source` is its own frame's window; the frame accepts one only from `window.parent` at an
origin the server named in the shell (`window.CANOPY_DV.origins`).

## What keeps it safe

**The frame can do nothing on its own.** Opaque origin: no canopy cookies, storage or DOM.
Its CSP: `default-src 'none'`, `connect-src 'none'` (no fetch, XHR, WebSocket, EventSource),
scripts and styles only from canopy's own build (plus `'unsafe-eval'` to run the compiled code,
and a per-response nonce for the one inline script), images only `data:`/`blob:`,
`frame-ancestors` canopy's own origins. `DynamicViewFrameMiddleware` answers before sessions and
auth, so the response never carries a `Set-Cookie`.

**Every read is a catalog read, as the viewer, in the view's workspace.** `resolveRead`
(bridge.ts) refuses an unknown model, a parameter the model does not list, a non-scalar value, a
path parameter that is not a plain identifier, or any attempt to name the workspace — `{workspace}`
is always the view's own. The page then GETs `/api/w/<workspace>/<path>` with the viewer's
session, so the route's own gate decides what comes back. A view can show nothing its viewer could
not already read, and it cannot write anything. `services.create`/`update` check declared sources against
the same catalog, so a bad source is refused when the view is written, not on every draw.

**What is left, said plainly.** Data handed to the frame can leave it — a WebRTC connection, or
the code navigating its own frame elsewhere (the page stops answering a frame that reloads, but the
navigation itself is not blocked). So a view's author can see what the view shows its viewers. What
the sandbox removes is the author acting AS the viewer, or reaching anything the view does not show.
That is why the catalog leaves out anything secret (vaults, tokens, credentials, mail bodies,
transcripts): put a read in it only if showing it to everyone who can open the view is fine.
`test_the_catalog_reads_nothing_secret` holds the list to that.

**Who may change a view.** Reading a view needs membership of its workspace. Creating, changing,
restoring or deleting one needs `agent.work` (editor) in its workspace, or edit rights on its
project (`agents.gates.may_edit_project`). Moving a view needs those rights where it lands as
well, so a project lead cannot put a view on the workspace. Every save is a new version; a restore is a new
version too, so history only grows.

## Writing one (usually an agent)

`get_dynamic_view_guide` returns the contract, the catalog and an example; `create_dynamic_view`
saves one (`workspace`, optional `project` handle, `title`, `code`, `sources`). The props:

- `data[alias]` — each declared source's result, or `{error}`.
- `scope` — `{workspace, project, view, viewer}`.
- `canopy` — `read(model, {params, path})`, `refresh()`, `navigate(path)`.

The kit (globals, no imports): `Card`, `Stat`, `Badge`, `Table`, `BarChart`, `Link`, `Empty`,
`Loading`, `Grid`, `Stack`, and React's hooks by name. Tailwind classes canopy uses plus a safelist
(`frame/frame.css`) work; canopy's semantic colour tokens, never raw palette colours.

## Adding to the catalog

One `Model(...)` line in `catalog.py`: a name, the REST read's path relative to `/api/w/<ws>`,
a description, and the query parameters a view may pass. The frontend reads the catalog from the
guide route, so nothing else changes. Before adding one, ask whether everyone who can open a view
should see what it returns — the author will.
