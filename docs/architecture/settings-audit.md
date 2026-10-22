# Settings audit

Every change to a setting leaves one `SettingChange` row (`apps/events/models.py`) that
names who made it, through what, what they sent and, where the route knows it, the
state before and after. There is one plumbing for it, `apps/events/audit.py`. No route
writes its own audit record.

## What counts as a setting

A route that changes how something is configured says so at the route:
`@gate(..., setting=True)` (`apps/api/route_gates.py`). That covers who may do what
(members, roles, admins, invites, access and admission rules), where work runs (runner routing, workspace order, pairing, pausing,
retiring), credentials and integrations (vaults, tokens, GitHub, Salesforce, Slack,
mailboxes, connected apps), what an agent or workspace is set to do (turn mode,
schedules, sender trust, retention), sharing and visibility, and a person's own
settings (tokens, preferences, memory grants).

Every other write route names its kind instead, `@gate(..., kind="...")`, from
`WRITE_KINDS`: content, conversation, turn, runner-protocol, action, person-data,
telemetry, request, or ephemeral-credential. `tests/test_every_route_declares_its_gate.py`
fails a write route that is neither, and a read that is either (`@gate` refuses both at
once). When unsure, classify it as a setting.

## How it works

- `audited` is registered once on the API (`api.add_decorator` in `apps/api/api.py`),
  so it wraps every route. It reads the view's `@gate`; for a route not declared
  `setting=True` it does nothing.
- For a setting route it runs the view inside `transaction.atomic()` and writes the row
  inside the same transaction. If the row cannot be written, the change rolls back.
- The web UI, the `canopy` CLI, scripts and MCP tools all reach a setting through the
  same routes. An MCP tool is the route, called in-process (`docs/architecture/mcp-surface.md`).
  So `via` is the only thing that differs: `ui` for a browser session, `mcp`, `cli`
  when `X-Canopy-Client` says canopy-cli, `api` for any other token, `runner`, and `unknown` when no credential type is known.
  The credential is verified. `client` and `user_agent` are only what the caller
  claimed.
- Nothing is recorded for a refused write (an `HttpError`, or a 4xx/5xx `Status`).
- The request body is stored with every secret-named key withheld, at any depth
  (`audit.redact`). Credentials are named by type, id and label, never carried.

## Before and after

A handler that knows the state it replaced reports it with
`audit.note_change(scope=, before=, after=)`. Routing does this (`apps/harness/routing_audit.py`)
because its writes delete and re-create rows that carry no author, so the previous rule
is otherwise gone. When every change a request noted is a no-op, such as re-sending a
rule unchanged, nothing is recorded. A route that notes nothing still gets its row; the
request body is then the record of what was set. Adding `note_change` to more handlers
is how a setting gets a before/after.

## Reading it

`GET /api/events/setting-changes` (MCP tool `list_setting_changes`) uses the same rule
as the MCP call log. A workspace admin (`logs.read`) sees every change in that
workspace, and anyone sees the changes they made themselves. A person's own settings
belong to no workspace (`workspace` is NULL) and are readable only by that person.
NULL never widens who can read a row. Filters: `operation` (prefix, e.g. `set_agent`),
`target` (substring: an agent slug, an address), `actor` (exact email), `since_minutes`, `limit`
(default 100, at most 500), newest first.

The rows are append-only. No route edits or deletes them.

## Related logs

- `MCPAuditLog`: one row per MCP tool call, including failed and rate-limited ones.
  It is a call log, not a change log.
- `Event` (`GET /api/events/`): operational events and faults.
