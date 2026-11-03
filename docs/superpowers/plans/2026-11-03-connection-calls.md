# Connection Calls From Screens and Apps (Part 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A dynamic view or workspace app page can call Salesforce through canopy-web, within an allowlist, as it declared, with writes needing a real click.

**Architecture:** One route, `POST /api/connections/{provider}/request`, resolves the surface and whose connection, checks the declaration and the allowlist, gets a token from `server_tokens.access_token`, and makes the call. The sandboxed frame reaches it only through the page around it, which holds the session and checks a click itself.

**Tech Stack:** Django + django-ninja, `requests`, React/TypeScript postMessage bridge.

**Spec:** `docs/superpowers/specs/2026-11-03-connections-server-use-calls-jobs-design.md` (Part 2); allowlist and declaration shape from §3 of `docs/superpowers/specs/2026-10-28-projects-as-products-design.md`.

**Worktree:** `~/emdash/worktrees/canopy-web-connections-server`, branch `feat/connection-calls`, from `origin/main` at 9791c77f.

## Global Constraints

- Every route has `@gate` directly above its def; docstrings are published OpenAPI; regenerate `frontend/src/api/generated.ts` as CI does.
- `apps/harness` and `apps/agents` never import `apps/connections`. `apps/dynamic_views` and `apps/workspace_apps` may import only `apps.connections.declarations`, or the import guard's allowed equivalent.
- The route takes only a signed-in person's session: a PAT or OAuth bearer token is refused 403.
- Salesforce only. Google answers 409 `provider_not_supported` with a sentence.
- A connection comes from the surface's own workspace. `as` is `viewer` (default) or `shared`.
- Public app pages never call. App pages call only on the `member` door.
- 300 calls a minute per person per provider, 30 s provider timeout, 5 MB response cap, 1 MB request body cap. No redirects followed. No provider call inside a database transaction.
- No token in any event, log, exception or response. A provider's own response headers are not returned: only status and JSON body.
- A limited connection (`expires_at` or `owner_turns_only`) is never used here: `server_tokens` already refuses it.
- Plain sentences in every refusal; no jargon on a screen.
- No deploy. No AWS.

## Review Focus

1. A view's code forging a click: the frame posts a message saying the user clicked. The page must decide from its OWN `navigator.userActivation.isActive`, which a frame click propagates to ancestors and a script cannot set; absent API fails closed. (Task 5)
2. A path trying to escape the allowlist: `../`, encoded `%2e%2e`, double slashes, a full URL, `sobjects/Lead/../../..`, a query smuggled in the path. Refused before any token is read. (Task 1)
3. A saved revision narrowing or widening declarations: the server checks against the revision the page drew, not the latest; `as: shared` stops at a widened revision until re-approved. (Tasks 2, 3)
4. A Salesforce 401 because its session ended: one forced refresh and one retry, then a plain 409. A write is retried only on 401, never on a timeout, since it may have landed. (Task 3)
5. Salesforce down or slow, KMS unavailable, a token waiting on a lease: each a plain sentence through the bridge, never a hung frame or 500. (Tasks 3, 5)

---

### Task 1: The allowlist

**Files:** Create `apps/connections/calls.py`. Test: `tests/test_connection_calls_allowlist.py`.

**Interfaces:**
- `calls.Rule(method: str, pattern: str, kind: "read"|"write", name: str)`.
- `calls.match(provider: str, method: str, path: str) -> Rule` raises `calls.NotAllowed` with a sentence. `path` is relative to the API version root (`services/data/<SF_API_VERSION>/`), for example `query`, `sobjects/Lead/00Q...`.
- `calls.build_url(provider, instance_url, path, query) -> str`, using the provider's host rules, never `path` for the host.

Salesforce rules are exactly the list in projects spec §3: `GET query`, `GET search`, `GET sobjects/*/describe`, `GET|POST|PATCH|DELETE sobjects/{type}/{id?}`, `GET|POST analytics/reports…`, `GET analytics/dashboards…`, `POST actions/custom/flow/{name}`, `POST apexrest/{name}` (served under `services/apexrest/`), `GET chatter/users/me`, `GET limits`, `GET ui-api/…`. Composite is out.

- [ ] Tests: each rule matches a real example and its kind; `test_refuses_traversal` over `../`, `%2e%2e/`, `%2E%2E`, `..%2f`, `//evil.com/x`, `https://evil.com`, `sobjects/Lead/../../x`, `query?x=y` in the path, a backslash, a control character; `test_refuses_composite`; `test_refuses_wrong_method`; `test_google_not_supported`; `test_build_url_keeps_host`, including a dotted `path` trying to change it.
- [ ] Run; they fail. Implement: percent-decode once, then refuse any `.`/`..` segment, empty segment, `%`, `?`, `#`, `\`, control character. Run; they pass. Commit.

### Task 2: Declarations

**Files:** Create `apps/connections/declarations.py`. Modify `apps/dynamic_views/models.py` (+migration), `services.py`, `schemas.py`; `apps/workspace_apps/manifest.py`; `apps/mcp` view tool schemas if they list fields; the dynamic-view guide text. Test: `tests/test_connection_declarations.py`.

**Interfaces:**
- `declarations.validate(raw) -> list[dict]`, raising `ValueError`. Shape: `[{provider, as: "viewer"|"shared", agent?: slug, reads: [{method, path}], writes: [{method, path}]}]`. Each entry must pass `calls.match` with a pattern form; `as: "shared"` requires `agent`; at most 5 entries, 50 lines each.
- `declarations.allows(declared: list[dict], provider, as_, agent_slug, rule, path) -> bool`.
- `DynamicView.connections` and `DynamicViewRevision.connections`, snapshotted as `agent_tasks` is; `DynamicViewOut` carries it; a top-level `connections` key on the app manifest, validated by `manifest.py`, composed through overlays unchanged.

- [ ] Tests: `test_valid_declaration_saved_and_snapshotted`; `test_declaration_outside_allowlist_refused`; `test_shared_needs_agent`; `test_limits`; `test_revision_keeps_old_declaration`; `test_rollback_restores_declaration`; `test_app_manifest_connections_validated`; `test_allows_read_and_write_pattern`; `test_allows_refuses_undeclared_write`.
- [ ] Run; they fail. Implement. Run; they pass, with the migration check. Commit.

### Task 3: The route

**Files:** Modify `apps/connections/server_tokens.py` (add `forget(conn)`, public), `api.py`, `schemas.py`, `problems.py`; move `_over` from `apps/embedding/rate_limit.py` to `apps/common/rate_limit.py` as `over(key, limit, window)`, importing it back. Create `apps/connections/call_service.py`. Test: `tests/test_connection_calls_route.py`.

**Interfaces:**
- `POST /api/connections/{provider}/request`, body `{method, path, query?, body?, surface: {kind: "view", id, version} | {kind: "app", workspace, key}, as?, agent?, operation_id?}`; response `{status: int, body: any}`.
- `call_service.call(user, provider, payload) -> dict`. Steps, in order: refuse a non-session caller; resolve the surface and check the person may open it (a view's existing read gate; an app's `member` door); `calls.match`; `declarations.allows` against that revision (a view's named `version`) or the live manifest; pick the connection (`as: viewer`: `services.connection_for_person(user, provider, workspace=ws)`, `ambiguous` → 409 with a sentence; `as: shared`: Task 4's share); a write needs a live operation (Task 4); rate limit; `server_tokens.access_token`; the call, no redirects, capped; on 401 once `forget`, get the token, retry; audit.
- A read is a log line. A write is an event `connection_call` with viewer, connection, surface, revision, operation id, method and path, never body, query or response.
- Refusals: `not_connected`, `server_use_off`, `ambiguous`, `not_declared`, `not_allowed`, `provider_not_supported`, `rate_limited` (429), `provider_unavailable` (502), `session_ended` (409) when a retry also gets 401.

- [ ] Tests: `test_viewer_read_returns_status_and_body`; `test_salesforce_error_status_passes_through`; `test_pat_refused`; `test_other_workspace_connection_not_used`; `test_ambiguous`; `test_undeclared_refused_before_token`; `test_old_revision_declaration_is_the_one_used`; `test_public_app_door_refused`; `test_off_switch_sentence`; `test_limited_connection_refused`; `test_rate_limit_429`; `test_response_over_5mb_refused`; `test_no_redirect_followed`; `test_401_forces_one_refresh_and_retry`; `test_timeout_not_retried`; `test_no_token_in_events_or_logs`; `test_kms_unavailable_sentence`.
- [ ] Run; they fail. Implement. Run; they pass, plus the gate lint and the embedding rate-limit tests. Regenerate types. Commit.

### Task 4: Operation leases and shares with projects and apps

**Files:** Modify `apps/connections/models.py` (+migration), `call_service.py`, `api.py`, `schemas.py`, `services.py`. Test: `tests/test_connection_operations.py`, `tests/test_connection_surface_shares.py`.

**Interfaces:**
- `ConnectionOperation(id, user, surface_kind, surface_ref, revision, writes_used, expires_at)`; `POST /api/connections/operations {surface}` → `{operation_id, expires_at, writes}`; expires 120 s, cap 500 writes, atomic increment. Expired rows are deleted by the existing connections sweep.
- `ConnectionSurfaceShare(connection, kind: "project"|"app", ref, approved_reads, approved_writes, approved_by, created_at)`, agent-owned connections only (refused for a person's, `ValueError`). `GET|POST|DELETE /api/connections/{id}/surface-shares`, agent admin only.
- `as: "shared"` requires a share whose approved lists cover what this revision declares for that provider and agent; any uncovered line → 409 `needs_approval`. A viewer of the surface may call; the audit line records viewer and connection.

- [ ] Tests: `test_write_needs_operation`; `test_operation_for_another_surface_refused`; `test_operation_for_another_person_refused`; `test_expired_operation_refused`; `test_cap_500`; `test_person_connection_cannot_be_shared`; `test_non_admin_cannot_share`; `test_shared_read_by_member`; `test_shared_refused_without_share`; `test_widened_revision_needs_approval`; `test_narrowed_revision_still_works`; `test_disconnect_removes_shares`; `test_project_view_covered_by_project_share`.
- [ ] Run; they fail. Implement. Run; they pass, with the migration check. Regenerate types. Commit.

### Task 5: The bridge

**Files:** Modify `frontend/src/dynamicViews/protocol.ts`, `frame/main.tsx`, `DynamicViewFrame.tsx`, `frontend/src/workspaceApps/AppFrame.tsx`. Create `frontend/src/dynamicViews/connectionCalls.ts` and test. Note a `connections` message already exists for a different purpose; the new one is `connection-call`.

**Interfaces:**
- In the frame: `canopy.connection.request({provider, method, path, query?, body?, as?, agent?}) -> Promise<{status, body}>`.
- `connectionCalls.ts`: `handleConnectionCall(request, ctx) -> Promise`: refuses undeclared locally; for a write, reuses a live operation for this frame or asks for one only if `navigator.userActivation?.isActive` on the PAGE is true; otherwise rejects "This needs a click.". The message's own claims about clicks are ignored. An operation is forgotten at expiry or when the view changes.
- Answers come back as `read-result`. A public door answers "a public page can't use connections" and asks nothing.

- [ ] Tests: `read goes through`; `undeclared refused with no request`; `write with no page activation refused with no request` (Review Focus 1); `a message claiming a click changes nothing`; `absent userActivation fails closed`; `write with activation asks for one operation and reuses it`; `expired operation asks again only with activation`; `public door refused`; `refusal sentence reaches the frame`.
- [ ] Run; they fail. Implement. Run vitest for those paths, tsc and eslint; pass. Commit.

### Task 6: Eva's Accounts tab, the view's declared list, docs, and everything

**Files:** Modify `frontend/src/pages/agents/AgentConnectionsPanel.tsx` and test; `frontend/src/dynamicViews/ProjectViewsSection.tsx` (show declared connections beside `DeclaredTasks`); `docs/architecture/dynamic-views.md`, `workspace-apps.md`, `api-surface.md`, `vault.md`; the dynamic-view guide; the `canopy:dynamic-view` skill note in the report for a follow-up in the canopy repo. Remove the deploy.md hold on the key only when Part 3 isn't needed for it: state that the key stack may now proceed.

Copy on the tab: "Project screens that may use {agent}'s account", each with what it may read and write in plain words, an "Approve changes" button when `needs_approval`, and "Stop". One sentence: "Anyone who can open that screen acts as {agent} in Salesforce."

- [ ] Tests: list, add, approve, stop; non-admin sees no controls; `needs_approval` shows the button.
- [ ] Run; they fail. Implement. Then the full backend, runner, frontend and e2e suites as CI does. Everything passes.
- [ ] Commit.
