# API: events, feedback, system and plugin catalog

Routes for the fleet event log, feedback, timeline, beta requests, system, MCP and the agent plugin catalog. Part of the [api surface](api-surface.md) topic; the index there lists every part.

## Feedback (`apps/feedback`) — what someone said, from any channel
Framework tier, generic over its target (`target_kind` + `target_ref` strings, never an FK). Feedback is input to a judgement, not work: the app emits **no signal** (no ask, push or timeline event; `tests/test_feedback_emits_nothing.py`). It has no pollers or connectors — an agent reads a channel and POSTs.
- `POST /api/feedback/` — Ingest (batch, atomic; editor+). Idempotent per `(channel, source_ref)` (partial unique index excluding blanks). `submitted_by` is the caller, never the external author.
- `GET /api/feedback/?target_kind=&target_ref=&state=&channel=` — The pool, in your workspaces.
- `POST /api/feedback/{id}/resolve` — Record a disposition (editor in its workspace). The only mutation.

## Events (`apps/events`) — the fleet log of actions and errors
Framework tier, generic over its producer (`source` + `kind` strings). Where an operational fault lives when it belongs to no `Turn`. A log, not a queue: emits **no signal** (`tests/test_events_emits_nothing.py`); a turn sweeps it. `Event.workspace` is NOT NULL.
- `POST /api/events/` — Record (batch, atomic; editor+). Repeats of `(workspace, source, key)` coalesce onto one row (`count`, `last_seen_at`); a blank `key` never coalesces; a row with no `source` is dropped. The workspace is the pinned one, else — for a box posting with its runner token — `Runner.workspace`, else the caller's sole workspace (422 when there is none). A runner token's `claude_auth_required` / `claude_usage_exhausted` / `claude_credit_exhausted` / `claude_api_key_fallback` also mails and pushes the runner's approvers, once per (runner, alert) per UTC day (`apps/harness/credential_notify.py`) — except `claude_auth_required` (or `claude_credit_exhausted`) from a runner whose health reports a signed-out (or out-of-credit) `claude.probe.*` check, which reaches people as the `runner_credential` alert instead.
- `GET /api/events/?source=&kind=&level=&since_minutes=&limit=` — Admin+ (`logs.read`). `source`/`kind` are prefix matches; newest-touched first.
- Session lifecycle: every start, stop and change to who can see a session or where it is listed is one `session.*` event (`source` `canopy_sessions.lifecycle`) with ids, actor, turn and before/after only — `?kind=session.` reads a workspace's. Kinds and producers: `docs/architecture/chat-sessions.md` § A session's lifecycle is in the event log. `agent.canopy_token.issued` names the turn's conversation in `session` (blank only before an agent turn's runner records one) and `scope` (`turn`/`session`).
- `GET /api/events/setting-changes?operation=&target=&actor=&since_minutes=&limit=` — The settings audit trail (`SettingChange`, `docs/architecture/settings-audit.md`), newest first: changes in workspaces where you are admin+, plus your own. `operation` is a prefix, `target` a substring, `actor` an exact email; `limit` ≤ 500 (default 100).
- `GET /api/events/mcp-calls?tool=&failed=&since_minutes=&limit=` — The MCP audit log (`MCPAuditLog`), newest first: every call in workspaces where you are admin+, plus your own calls anywhere. `tool` is a prefix; `failed` keeps errors. A call made under an act-as-you permission on canopy (MCP or REST) names the person in `user_email` and the agent in `acting_agent`, with the grant id (`act_as_grant`).
- No mutation route. Retention: 30 days untouched (anchored on `last_seen_at`), pruned daily by the `events.prune` tick; `manage.py prune_events` by hand.

## Timeline (`apps/timeline`)
- `GET /api/timeline/` — Team activity timeline: cursor-paginated aggregation over other apps via a string registry; link-out only.

## Beta requests (`apps/beta_requests`) — the public site's request-access form
- `POST /api/beta-requests` — Anonymous (`auth=None`, allowlisted). `{email, reason, website}`; `website` is a honeypot (answered `ok`, nothing kept). The row is written first; the email to `CANOPY_BETA_REQUESTS_TO` (Reply-To the requester) is best-effort, outcome in `notify_result` (`sent`/`not_configured`/`failed`/`skipped`). A repeat within 24h is kept but not re-mailed; 5 per client address per hour (counted from the table); the response is identical for first and repeat requests. Grants nothing and creates no user.
- `GET /api/beta-requests?status=` · `GET /api/beta-requests/{id}` — Reviewers only (the address requests are mailed to, and superusers).
- `POST /api/beta-requests/{id}/invite` — Invite the requester to `workspace` at `role` (an ordinary invite: needs `members.manage` there, grants only below your role); returns `email_status`. 409 if already answered.
- `POST /api/beta-requests/{id}/decline` — Close it; emails nobody. 409 if already answered.

## System (`apps/system`)
- `GET /api/system/overview` — Capability catalog: the canopy plugin's skills/agents/commands, read live from the plugin.
- `GET /api/system/{kind}/{name}` — One capability's detail (drives the `/system` Workflows view).
- `GET /api/system/public-stats` — Anonymous (`auth=None` **and** listed in `apps/common/middleware.py` `PUBLIC_PATH_PREFIXES`; `tests/test_public_routes_reachable.py` pins the latter). Five aggregate integers (agents, skills, runners online, turns executed, published demo packages) for the public explainer; integers only, shape closed by type in `tests/test_public_stats.py`, cached 60s. Accepted residual: the counters reveal fleet activity, and `demos_published` counts all tenants and visibilities.

## MCP (`apps/mcp`, mounted at `/api/mcp/`)
A FastMCP 4.x Streamable-HTTP app mounted in `config/asgi.py`, not a Ninja router. Every REST route above is a tool (generated from the OpenAPI schema, dispatched in-process as the caller); clients sign in with OAuth or a per-user PAT (`CanopyPATVerifier`); every call writes an `MCPAuditLog` row (readable at `GET /api/events/mcp-calls`) and writes are rate-limited. Hand-written tools exist only for what is not a route. Full surface, exclusions and auth: `docs/architecture/mcp-surface.md`.

## Agent plugin catalog (`apps/plugins/views.py`, bare views)
- `GET /plugins/marketplace.json` — The caller's one Claude Code catalog, marketplace `canopy-agents`: a `github` source per agent plugin in every workspace they belong to (fetched with their own git; canopy lends no GitHub access). Bearer canopy token; no identity → 401 naming the header (never a sign-in redirect); no memberships → empty catalog. `GET /w/{ws}/marketplace.json` · `GET /w/{ws}/plugins/…` — retired, 410 naming the new URL. Contract: `docs/architecture/plugin-marketplace.md`.
