# Connections (Salesforce first) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person or an agent connects a Salesforce account to canopy once; canopy-web holds and refreshes the grant; access tokens reach only a runner turn of that agent and the person's own CLI.

**Architecture:** New Django app `apps/connections`, a generalisation of `apps/github_connect`: one `Connection` row per (owner, provider, account), owner a user XOR an agent; a provider registry in code; a request-free service layer (`connections.services`) that every route calls; bare views for the browser OAuth legs; Ninja routes for status, disconnect and token handout; a Settings panel and an agent-page panel; `canopy cred salesforce-token` in the canopy CLI.

**Tech Stack:** Django 5 + Django Ninja, `requests`, Fernet (`apps.common.encryption`), pytest; React + vitest (frontend); Python CLI in `dimagi-internal/canopy` (`src/orchestrator/cred_cli.py`).

**Spec:** `docs/superpowers/specs/2026-10-28-projects-as-products-design.md` §1 and "What changes in canopy's rules" (1). Pattern to copy: `apps/github_connect/` (read its docstrings first — the rotation race and the 200-with-`error` case apply verbatim).

## Global Constraints

- Tokens are encrypted with `encrypt_secret`/`decrypt_secret` (`apps/common/encryption.py`); never logged, never in an event payload, never in a response except the two token-handout routes.
- Only canopy-web refreshes a grant: inside `transaction.atomic()` with `select_for_update()` on the row; cached access token reused until `ACCESS_TOKEN_MARGIN = timedelta(minutes=5)` before expiry.
- Salesforce rotates refresh tokens: always store the new `refresh_token` when the refresh response carries one.
- Every outbound HTTP call has `timeout=HTTP_TIMEOUT` (15 s).
- Every Ninja route declares `@gate(...)` from `route_gates.VOCABULARY`; writes are `setting=True` or name a `kind`.
- Settings: `SALESFORCE_CLIENT_ID`, `SALESFORCE_CLIENT_SECRET`, `SALESFORCE_LOGIN_URL` (default `https://login.salesforce.com`) via `env(...)` in `config/settings/base.py`. Scopes `api refresh_token`. `SF_API_VERSION = "v65.0"`.
- No fallback between owners: an agent turn uses the agent's connection only; a person's CLI uses the person's only.
- No IP restriction assumption anywhere (spec §1).

## Review Focus

1. **Salesforce answers 400 `invalid_grant` on refresh** (revoked, rotated elsewhere, user deactivated) → `refresh_failed_at` stamped, the token routes return 409 `reconnect_required` with the reconnect URL, the panel says "Reconnect". Test in Task 2.
2. **Two refreshes race** (two workers, CLI + turn) → exactly one exchange at Salesforce; the second caller gets the first's token. Test in Task 2 (second call inside the lock sees a fresh cached token and makes no HTTP call).
3. **The same Salesforce user connected twice by one owner** (reconnect after revoke) → the existing row is updated, not duplicated (unique on owner+provider+account_id). Test in Task 2.
4. **A non-admin hits the agent connect start URL, or the callback's state names an agent the caller no longer administers** → 403 / refused, nothing stored. Test in Task 3.
5. **A turn whose agent has no Salesforce connection, or a caller-confined turn** → 409 `not_connected` / 404, never another owner's token. Test in Task 5.

---

### Task 1: The `Connection` model and the provider registry

**Files:**
- Create: `apps/connections/__init__.py`, `apps.py`, `models.py`, `providers/__init__.py`, `providers/base.py`, `providers/salesforce.py`, `migrations/0001_initial.py`
- Modify: `config/settings/base.py` (INSTALLED_APPS; the three `SALESFORCE_*` settings next to `GITHUB_APP_*`)
- Test: `tests/test_connections_model.py`

**Interfaces:**
- Produces:
  - `Connection` fields: `user` (FK user, null), `agent` (FK `agents.Agent`, null), `provider` (char 40), `account_id` (char 255), `account_label` (char 255, display only), `instance_url` (URL, blank), `scopes` (char 500), `refresh_token_enc`, `access_token_enc` (text, blank), `access_token_expires_at`, `refresh_failed_at`, `last_used_at` (datetime, null), `connected_by` (FK user), `created_at`, `updated_at`. Constraints: check exactly one of `user`/`agent`; unique (`user`,`provider`,`account_id`) and (`agent`,`provider`,`account_id`). Property `needs_reconnect -> bool` (no refresh token, or `refresh_failed_at` set). Property `owner_label -> str`.
  - `ConnectionGrant(connection FK, agent FK, granted_by FK user, created_at)`, unique (connection, agent): an **agent's** connection used by another agent — today's case is Eva's Salesforce user (eva@dimagi-ai.com) used by Echo, held as `salesforce` in the zero-knowledge workspace vault "Salesforce (Eva's account)". Only for agent-owned connections (a person's connection is never granted to an agent here). Granting needs admin of both agents.
  - `providers.get(name: str) -> Provider` (raises `UnknownProvider`); `providers.ALL: dict[str, Provider]`.
  - `Provider` (dataclass/protocol in `base.py`): `name`, `title`, `scopes: tuple[str, ...]`, `rotates_refresh_token: bool`, `is_configured() -> bool`, `authorize_url(state: str, redirect_uri: str, code_challenge: str) -> str`, `exchange_code(code: str, redirect_uri: str, code_verifier: str) -> TokenResponse`, `refresh(refresh_token: str) -> TokenResponse`, `identity(token: TokenResponse) -> Identity`, `revoke(token: str) -> None`.
  - `TokenResponse(access_token: str, refresh_token: str, expires_at: datetime | None, instance_url: str, raw_id: str)`; `Identity(account_id: str, label: str)`.
  - Salesforce: authorize/token/revoke at `{SALESFORCE_LOGIN_URL}/services/oauth2/{authorize,token,revoke}`; PKCE S256 plus client secret; identity from the `id` URL in the token response (`GET` it with the access token → `user_id`, `username`); `account_id = "<organization_id>/<user_id>"`; Salesforce returns no `expires_in`, so `expires_at = now + 2h` unless the org's session policy is read (builder may hardcode 2h and refresh on 401).

- [ ] **Step 1: Write failing tests** `test_grant_only_for_agent_owned_connections`, `test_exactly_one_owner_enforced` (IntegrityError for both or neither), `test_unique_per_owner_provider_account`, `test_needs_reconnect_states`, `test_registry_returns_salesforce_and_rejects_unknown`, `test_salesforce_not_configured_without_settings`.
- [ ] **Step 2:** `pytest tests/test_connections_model.py -q` → FAIL (no module).
- [ ] **Step 3:** Implement model, migration (`makemigrations connections`), registry and `SalesforceProvider` per the Interfaces block.
- [ ] **Step 4:** Tests pass; `python manage.py makemigrations --check` clean.
- [ ] **Step 5:** Commit `feat(connections): Connection model and the Salesforce provider`.

### Task 2: The service layer — connect, refresh, disconnect

**Files:**
- Create: `apps/connections/services.py`
- Test: `tests/test_connections_services.py` (Salesforce faked at the `requests` boundary, as `tests/test_github_connect.py`)

**Interfaces:**
- Consumes: Task 1.
- Produces (all request-free; take a `session` mapping where state is needed):
  - `begin(session, *, provider: str, owner: Owner, redirect_uri: str) -> str` — PKCE verifier + `state` parked in `session["connections_oauth"]` keyed by state, with `{provider, owner_kind, owner_id, verifier, return_to}`; returns the authorize URL. Raises `NotConfigured`.
  - `complete(session, *, provider: str, code: str, state: str, redirect_uri: str, by) -> Connection` — pops the state (single use; missing/mismatched → `ConnectionError("bad_state")`), exchanges, reads identity, upserts on (owner, provider, account_id), clears `refresh_failed_at`, records event `connections.connected`.
  - `Owner` = `tuple[Literal["user","agent"], int]`; helper `owner_of(conn) -> Owner`.
  - `access_token_for(conn: Connection) -> AccessToken` — `AccessToken(token: str, instance_url: str, expires_at: datetime, account_label: str)`; cached-or-refresh under the row lock; on provider rejection stamps `refresh_failed_at` and raises `ReconnectRequired`; stores a rotated refresh token; sets `last_used_at`.
  - `disconnect(conn: Connection, *, by) -> None` — best-effort `provider.revoke(refresh_token)` (failure logged, not raised), delete row, event `connections.disconnected`.
  - `connections_for(owner: Owner) -> QuerySet[Connection]`.
  - `connection_for_agent(agent, provider: str) -> Connection` — the agent's own connection for the provider, else one granted to it (`ConnectionGrant`); none → `NotConnected`; more than one candidate → `ConnectionError("ambiguous")`. Never a person's connection.
  - `grant(conn, agent, *, by) -> ConnectionGrant` / `revoke_grant(grant, *, by)` — `by` must administer both agents; events `connections.granted` / `connections.grant_revoked`.
  - Exceptions: `NotConfigured`, `ConnectionError(code)`, `ReconnectRequired`, `NotConnected`.
- Events via `apps.events.services.record([...], workspace=…)` (agent's workspace for agents; none for people), payload: provider, owner, account_label — never tokens.

- [ ] **Step 1: Failing tests:**
  - `test_begin_parks_state_and_pkce` (URL has `code_challenge_method=S256`, `scope=api refresh_token`, state in session)
  - `test_complete_rejects_unknown_or_reused_state`
  - `test_token_error_with_200_is_failure` (body `{"error": "invalid_grant"}` → `ConnectionError`, nothing stored)
  - `test_complete_upserts_same_account` (Review Focus 3)
  - `test_cached_token_reused_without_http`
  - `test_refresh_stores_rotated_refresh_token`
  - `test_refresh_inside_lock_rechecks_cache` (Review Focus 2: patch so the row gains a fresh token before the lock is taken; assert zero token-endpoint calls)
  - `test_invalid_grant_stamps_reconnect` (Review Focus 1)
  - `test_disconnect_revokes_and_deletes_even_if_revoke_fails`
  - `test_no_token_in_event_payloads`
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `services.py` per Interfaces, mirroring `github_app.access_token_for`'s lock structure.
- [ ] **Step 4:** Tests pass.
- [ ] **Step 5:** Commit `feat(connections): connect, refresh under lock, disconnect`.

### Task 3: Browser legs — start and callback, for a person and for an agent

**Files:**
- Create: `apps/connections/views.py`
- Modify: `config/urls.py` (`auth/connections/<str:provider>/start/` name `connections_start`; `auth/connections/<str:provider>/callback/` name `connections_callback`)
- Test: `tests/test_connections_views.py`

**Interfaces:**
- Consumes: `services.begin`, `services.complete`.
- `GET|POST /auth/connections/<provider>/start/?agent=<slug>&return_to=<path>` — `@login_required`. With `agent`, the caller must be an admin of it (`agent.is_admin(request.user)`), else 403. `return_to` must be a same-site path (starts with `/`, not `//`), else `/settings`. Redirect URI built with `request.build_absolute_uri(reverse("connections_callback", args=[provider]))` (see `github_connect.views._redirect_uri` for why `reverse`).
- Callback: re-checks agent admin at completion (Review Focus 4), then redirects to `return_to?connection=<provider>&status=connected|error&reason=<code>`.

- [ ] **Step 1: Failing tests:** `test_start_redirects_to_salesforce_for_self`, `test_start_for_agent_requires_admin`, `test_callback_refuses_when_admin_revoked_between_legs`, `test_return_to_rejects_offsite`, `test_not_configured_redirects_with_reason`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Pass.
- [ ] **Step 5:** Commit `feat(connections): browser connect flow for people and agents`.

### Task 4: JSON routes — status, disconnect, my CLI token

**Files:**
- Create: `apps/connections/api.py`, `apps/connections/schemas.py`
- Modify: `apps/api/api.py` (`api.add_router("/connections", router)`; agent routes on the agents router prefix `/agents`)
- Test: `tests/test_connections_api.py`

**Interfaces:**
- `ConnectionOut {id, provider, title, account_label, instance_url, needs_reconnect, connected_at, last_used_at, connected_by_email}`; `ProviderOut {name, title, configured}`; `AccessTokenOut {access_token, instance_url, expires_at, account_label}`.
- `GET /api/connections/` → `{providers: [ProviderOut], connections: [ConnectionOut]}` mine — `@gate("self")`.
- `DELETE /api/connections/{id}` → 204, mine only (another's id 404) — `@gate("self", setting=True)`.
- `GET /api/agents/{slug}/connections` — `@gate("agent-admin")` (use `agent_for_admin`).
- `GET /api/agents/{slug}/connections` also lists connections granted to the agent (`granted_from: <agent slug>`).
- `PUT /api/agents/{slug}/connections/{id}/grants/{other_slug}` / `DELETE …` — share an agent-owned connection with another agent; `@gate("agent-admin", setting=True, step_up=True)`, caller must administer both.
- `DELETE /api/agents/{slug}/connections/{id}` — `@gate("agent-admin", setting=True, step_up=True)`; browser callers pass `stepup.require_recent_step_up` as `api_credentials.set_agent_credentials` does.
- `POST /api/me/connections/{provider}/token` → `AccessTokenOut` — `@gate("self", step_up="a person's own token for their own CLI; PAT only")`; refuses a browser session (403 `token_route_needs_pat`, check `stepup.is_browser(request)`); 409 `not_connected` / `reconnect_required` with `connect_url`; one connection per provider is assumed for v1 (several → 409 `ambiguous`, body lists account labels). Records event `connections.token_issued` (to: cli).

- [ ] **Step 1: Failing tests:** list/delete scoping, agent routes need admin, token route refuses browser session, token route 409 shapes, issued event recorded without the token.
- [ ] **Step 2–4:** FAIL → implement → PASS. Regenerate OpenAPI types (`make api-types` or the repo's documented command) so `frontend/src/api/generated.ts` has the routes.
- [ ] **Step 5:** Commit `feat(connections): status, disconnect and CLI token routes`.

### Task 5: Runner turn token

**Files:**
- Modify: `apps/harness/api_runners.py` (next to `turn_github_token`), `apps/harness/schemas.py` (`TurnConnectionTokenIn {provider}`, reuse `AccessTokenOut`)
- Test: `tests/test_connections_turn_token.py`

**Interfaces:**
- `POST /api/harness/runners/{runner_id}/turns/{turn_id}/connection-token` — `@gate("runner", "runner-holds-agent", kind="runner-protocol", step_up="runner protocol")`. Same turn lookup and `runner_may_hold_agent` re-check as `turn_github_token`; turn's agent via `delegations.turn_agent(turn)`; no agent → 409 `no_agent`; caller-confined turn (capability-confined) → 404; `services.connection_for_agent(agent, provider)` (its own, else one granted to it; never the initiator's) → `access_token_for`. Event `connections.token_issued` (to: turn, runner, agent).

- [ ] **Step 1: Failing tests:** `test_granted_connection_used_by_borrowing_agent` (Echo's turn gets Eva's token after a grant; not before),  `test_issues_agent_token_for_claimed_turn`, `test_never_initiators_connection` (initiator has one, agent none → 409 `not_connected`), `test_unclaimed_or_finished_turn_404`, `test_confined_turn_404`, `test_reconnect_required_409`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(connections): a turn gets its agent's Salesforce token`.

### Task 6: Settings and agent-page panels

**Files:**
- Create: `frontend/src/components/settings/ConnectionsPanel.tsx` (+ `.test.tsx`), `frontend/src/components/agents/AgentConnectionsPanel.tsx` (+ `.test.tsx`)
- Modify: the Settings page that renders `GitHubPanel` (add `ConnectionsPanel` beside it); the agent settings page (add `AgentConnectionsPanel` for admins)

**Interfaces:** reads `GET /api/connections/` / `GET /api/agents/{slug}/connections`; "Connect Salesforce" is a full-page navigation to `/auth/connections/salesforce/start/?return_to=<current path>[&agent=<slug>]` (prefix from the app's base path helper, as `GitHubPanel` builds its link); shows account label, instance, last used, "Reconnect" when `needs_reconnect`, "Disconnect" (agent: triggers step-up via the existing step-up hook); reads `?connection=…&status=…&reason=…` to toast the result; provider not configured → disabled button with "Not set up on this deployment".

- [ ] **Step 1:** Failing vitest: renders connected / not connected / needs reconnect / not configured; disconnect calls DELETE; connect link carries `return_to` and `agent`.
- [ ] **Step 2–4:** FAIL → implement → PASS (`npm test -- ConnectionsPanel AgentConnectionsPanel`).
- [ ] **Step 5:** Commit `feat(connections): Settings and agent panels`.

### Task 7: `canopy cred salesforce-token` (repo dimagi-internal/canopy)

**Files:**
- Modify: `src/orchestrator/cred_cli.py` (new subcommand beside `google-token`)
- Test: `tests/test_cred_cli.py`

**Interfaces:**
- `canopy cred salesforce-token [--agent SLUG]` — without `--agent`: `POST /api/me/connections/salesforce/token` with the human token (`_human_token()`); inside a runner turn (`_turn_id()` present) or with `--agent`: `POST /api/harness/runners/{rid}/turns/{tid}/connection-token {provider: "salesforce"}` with the runner's credentials (same path the Google turn token uses). Writes `{access_token, instance_url, expires_at, account_label}` with `_write_private` to `~/.canopy/connections/salesforce[/<agent>].json` (0600) and prints the path, never the token. Reuses a cached file until 5 minutes before `expires_at`. 409 bodies print their `detail` and `connect_url` and exit non-zero.

- [ ] **Step 1:** Failing tests: human path writes 0600 file and prints path; turn path uses the runner route; cache reuse; 409 prints connect URL, exit code non-zero; token never on stdout.
- [ ] **Step 2–4:** FAIL → implement → PASS (`pytest tests/test_cred_cli.py -q`).
- [ ] **Step 5:** Commit in the canopy repo `feat(cred): salesforce-token from canopy connections`.

### Task 8: Docs and the rule change

**Files:**
- Modify: `docs/architecture/vault.md` (new section "OAuth connections are server-held" stating rule 1 and pointing at `apps/connections`), `docs/architecture/api-surface.md` (the new routes, in its style), `CLAUDE.md` (one line under credentials if it lists them), `.env.example` / deploy docs for the three `SALESFORCE_*` settings.
- Test: `pytest tests/test_route_gates*.py -q` (every new route declares a gate) and the repo's docs checks if any.

- [ ] **Step 1:** Write the docs. **Step 2:** Run the gate/schema tests and the full `pytest -q -x` for touched apps. **Step 3:** Commit `docs(connections): server-held OAuth connections`.

---

## Migrating Eva's Salesforce credential (after this plan deploys)

Today: Eva's own Salesforce user's refresh token is `salesforce` in the zero-knowledge workspace
vault "Salesforce (Eva's account)", attached to Eva and Echo; each runner copies it to
`~/.eva/chrome-sales/.sf-creds.json` and refreshes it itself; `bin/eva-sf-creds mint` re-mints
headlessly with `SALESFORCE_LOGIN_PASSWORD` / `SALESFORCE_LOGIN_TOTP`. That token belongs to
chrome-sales's Connected App, so it cannot be imported: an admin of Eva connects once on
Eva's page, signing in as eva@dimagi-ai.com through canopy's Connected App, then grants it to
Echo. Eva's and Echo's tools switch to `canopy cred salesforce-token` (canopy-salesforce S1);
the vault entry and `bin/eva-sf-creds` are retired after that (eva repo change). The login
password and MFA seed are pasted secrets and stay in Eva's zero-knowledge vault only if
headless re-connect is kept (decide then; a revoked grant is rare and an admin can reconnect).

## Owner-gated, outside this plan's code

- Create the Salesforce Connected App (or External Client App) in Dimagi's org: callback `https://canopy.dimagi.com/auth/connections/salesforce/callback/` (+ staging), scopes `api refresh_token`, PKCE required, refresh-token rotation on. Then set the three settings in production. **Needs Jonathan.**
- Create the agents' dedicated Salesforce user(s). **Needs Jonathan.**
- Deploy canopy-web. **Needs Jonathan's approval.**
