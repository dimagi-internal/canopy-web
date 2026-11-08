# Settings audit

Every change to a setting leaves one `SettingChange` row (`apps/events/models.py`) naming who
made it, through what, what they sent and the state before and after. One plumbing writes it,
`apps/events/audit.py`. No route writes its own audit record.

## What counts as a setting

A route says so at the route: `@gate(..., setting=True)` (`apps/api/route_gates.py::gate`) —
who may do what, where work runs, credentials and integrations, what an agent or workspace is
set to do, sharing and visibility, a person's own settings. Every other write route names its
`kind=` from `route_gates.WRITE_KINDS`. `tests/test_every_route_declares_its_gate.py` fails a
write route that is neither, and a read that is either. When unsure, classify it as a setting:
an extra row costs nothing, a missing one is the gap the audit exists to close.

## How it works

- `audited` is registered once on the API (`api.add_decorator` in `apps/api/api.py`), so it
  wraps every route; for a route not declared `setting=True` it does nothing.
- For a setting route it runs the view inside `transaction.atomic()` and writes the row in the
  same transaction. If the row cannot be written, the change rolls back.
- The web UI, the `canopy` CLI, scripts and MCP tools all reach a setting through the same
  routes (an MCP tool is the route, called in-process), so `via` is the only thing that
  differs (`audit._via`). The credential is verified; `client` and `user_agent` are only what
  the caller claimed.
- Nothing is recorded for a refused write (`audit._refused`).
- The request body is stored with every secret-named key withheld, at any depth
  (`audit.redact`). Credentials are named by type, id and label, never carried.

## Before and after

Every setting route gets a before/after without code of its own. While `audited` runs it, each
row the route writes is captured (`apps/api/change_capture.py`) as one entry of `changes`
(`scope`, `before`, `after`, and `also_deleted` for what a delete cascaded to); the module
docstring states the shape.

- **What is seen:** `save()`, `Model.delete()`, `QuerySet.delete()`, `QuerySet.update()` and
  `bulk_create()` (`change_capture.install`). Deletes are not taken from delete signals: any
  delete listener turns off Django's fast delete for its model always, and deleting a
  workspace or an agent cascades through high-volume tables.
- **What is not:** `bulk_update()`, raw SQL, and m2m `add`/`remove`. A route that writes only
  that way reports with `audit.note_change`.
- **Not settings:** the audit's own rows, the logs and the rolling session
  (`audit._NOT_SETTINGS`).
- **Secrets:** a secret-named field, any `*_enc`, hash, digest or salt field, and binary fields
  are `[redacted]` (`audit._SECRET_KEY`, `audit._SECRET_FIELD`).

A handler that knows the state better reports it with `audit.note_change(scope=, before=,
after=)`, and its notes replace the captured rows. Routing does this
(`apps/harness/routing_audit.py`) because its writes delete and re-create rows, so a row-level
record would read as a delete and a create. When every change a request noted is a no-op, nothing
is recorded. A route whose writes changed nothing and that noted nothing still gets its row,
with empty `changes`; the request body is then the record of what was set.

## Reading it

`GET /api/events/setting-changes` (MCP tool `list_setting_changes`; filters on the route) uses
the same rule as the MCP call log (`audit.visible_to`): a workspace admin (`logs.read`) sees
every change in that workspace, and anyone sees the changes they made themselves. A person's own
settings belong to no workspace (`workspace` is NULL) and are readable only by that person.
NULL never widens who can read a row.

The rows are append-only. No route edits or deletes them.

## Related logs

- `MCPAuditLog` (`apps/mcp/models.py`): one row per MCP tool call, including failed and
  rate-limited ones. A call log, not a change log.
- `Event` (`GET /api/events/`): operational events and faults.

## Agent roles

Defining, editing, resetting and deleting an agent role (`apps/agents/api_roles.py`) are
settings changes like any other: `@gate("members.manage", setting=True, step_up=True)`, one
`SettingChange` per call, scope `agent-roles:<workspace>:<key>`, before and after the role's
closed permission set, and a summary that ends with the blast radius (how many lines, agents
and workspace defaults hold the role).
