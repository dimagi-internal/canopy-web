# API: events, feedback, system and plugin catalog

Rules the schema cannot express for the fleet event log, feedback, timeline, beta requests, system, MCP and the agent plugin catalog. Part of the [api surface](api-surface.md) topic. Routes, parameters and shapes are in the routers named below and in the OpenAPI schema.

## Feedback (`apps/feedback/api.py`)
Generic over its target (`target_kind` + `target_ref` strings, never an FK). Feedback is input to a judgement, not work: the app emits **no signal** (no ask, push or timeline event; `tests/test_feedback_emits_nothing.py`). It has no pollers or connectors; an agent reads a channel and POSTs. Ingest is idempotent per `(channel, source_ref)` and `submitted_by` is the caller, never the external author. `resolve` is the only mutation and needs editor in the feedback's workspace.

## Events (`apps/events/api.py`)
A log, not a queue: emits **no signal** (`tests/test_events_emits_nothing.py`), has no mutation route (a log you can edit is not a record), and `Event.workspace` is NOT NULL. Writing needs `events.write`, so a viewer cannot forge `runner.credential` alarms or bump a real event's count through coalescing; repeats of `(workspace, source, key)` coalesce, a blank key never does. The workspace is the pinned one, else (for a box posting with its runner token) `Runner.workspace` if the owner may still write there, else the caller's default (422 when none). A runner token's first credential event of the day also mails and pushes the runner's approvers (`harness/credential_notify.py`). Reading takes `logs.read`; a non-admin gets no rows, not a 403. Retention is the `events.prune` tick (`apps/retention/apps.py`). The settings audit trail (`list_setting_changes`, [settings-audit.md](settings-audit.md)) and the MCP call log (`apps/mcp/audit_api.py::list_mcp_calls`) are read on the same tier: a workspace's admins see its rows, anyone sees their own. Session lifecycle events: [chat-sessions.md § A session's lifecycle is in the event log](chat-sessions.md#a-sessions-lifecycle-is-in-the-event-log).

## Timeline (`apps/timeline`)
`GET /api/timeline/`: link-out aggregation over other apps through a string registry; no rule beyond membership.

## Beta requests (`apps/beta_requests/api.py`)
Submit is anonymous (`auth=None`) and grants nothing. A filled `website` honeypot is answered `ok` and kept nowhere. The response is identical for a first and a repeat request (a repeat within `REPEAT_WINDOW` is kept but not re-mailed; `IP_LIMIT` per client address per `IP_WINDOW`, counted from the table, `services.py`). Reading, inviting and declining are reviewers only (`services.may_review`: superusers and the address requests are mailed to), and everyone else gets 404. Invite is an ordinary invite: it also needs `members.manage` in the chosen workspace and grants only roles below the caller's. Answering twice is 409.

## System (`apps/system`)
`GET /api/system/public-stats` is anonymous and needs both halves: `auth=None` on the route **and** the path in `apps/common/middleware.py::PUBLIC_PATH_PREFIXES` (`tests/test_public_routes_reachable.py` pins the latter). Integers only; the shape is closed by type in `tests/test_public_stats.py`, so adding a non-integer field is a security change. What the counters reveal and why it is accepted: `apps/system/stats.py` module docstring.

## MCP (`apps/mcp`, mounted at `/api/mcp/`)
A FastMCP app mounted in `config/asgi.py`, not a Ninja router. Every REST route is a tool (generated from the OpenAPI schema, dispatched in-process as the caller); every call writes an `MCPAuditLog` row and writes are rate-limited. Surface, exclusions and auth: [mcp-surface.md](mcp-surface.md).

## Agent plugin catalog (`apps/plugins/views.py`)
Bare views, not Ninja routes: the address is fixed by what Claude Code fetches, and the catalog must not become an MCP tool. `GET /plugins/marketplace.json` is the caller's one catalog (`services.catalog`), filtered to their memberships; canopy lends no GitHub access. A request with no canopy identity gets a 401 naming the header, never a sign-in redirect (`LoginRequiredMiddleware` admits these paths for that reason). The `/w/<ws>/…` marketplace addresses answer 410 with the current URL. Contract: [plugin-marketplace.md](plugin-marketplace.md).
