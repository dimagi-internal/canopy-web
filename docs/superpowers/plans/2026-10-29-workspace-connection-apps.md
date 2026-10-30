# Workspace connection apps (+ Google) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move each connection provider's OAuth client (key, secret, options) from site-wide env settings into a per-workspace record that a workspace admin configures, with a per-workspace callback URL, and add Google (Gmail/Calendar) as the second provider.

**Architecture:** A new `ConnectionApp` row (workspace × provider) holds the client id, the Fernet-encrypted client secret, provider options and the chosen extra scopes. Every `Connection` points at the app that minted it, so refresh/revoke use that app's credentials. Providers become classes instantiated per app (`providers.for_app(app)`), not settings-reading singletons. The browser legs move to `/auth/connections/<provider>/<workspace>/start|callback/`, so each workspace registers its own redirect URI and a callback can only complete a flow begun against the same app (OAuth mix-up defence, RFC 9700 §4.4).

**Tech Stack:** Django 5 + django-ninja, React/TS (vitest), the canopy CLI (Python).

**Spec:** Decided in chat with Jonathan on 2026-10-29 (this plan records it): "this should be a workspace level connect, not site-wide"; "shouldn't the callback be workspace specific?"; "we should do this for gmail/google too". Earlier binding context: `.superpowers/sdd/2026-10-28-connections/context.md` (rulings still hold unless changed below) and `docs/superpowers/specs/2026-10-28-projects-as-products-design.md`.

## Global Constraints

- No site-wide provider settings remain: delete `SALESFORCE_CLIENT_ID|CLIENT_SECRET|LOGIN_URL|ACCESS_TOKEN_SECONDS` from `config/settings/base.py`, `.env.example`, docs. No fallback to env.
- Client secret: `encrypt_secret`/`decrypt_secret` (`apps/common/encryption.py`), column `client_secret_enc`; never returned by any route, never logged, never in an event. Responses carry `has_secret: bool`.
- Writing an app (create/update/delete) is gated `@gate("integrations", setting=True, step_up=True)` with `workspace_for(request.user, ws, perms.INTEGRATIONS)` and `stepup.require_recent_step_up(request)` when `stepup.is_browser(request)` — after the permission check, as `apps/connections/api.py` grants do.
- Callback path: `/auth/connections/<provider>/<workspace>/callback/` (workspace slug is the immutable PK). Start: `/auth/connections/<provider>/<workspace>/start/`. URL names `connections_start` / `connections_callback` keep their names with the new args.
- `begin` parks the app's pk; `complete` refuses (`bad_state`) unless the callback's provider AND workspace match the parked app.
- A person connects in a workspace they are a member of (`permissions.READ`); a person's token is usable only while still a member of the connection's app workspace (else `not_connected`). An agent connects with its own workspace's app (`agent.workspace`); grants across workspaces keep working (the owner connection's app refreshes).
- Deleting an app disconnects (revoke best-effort + delete) every connection made with it, recording `connections.disconnected` for each with `by` = the admin; the app's own create/update/delete record `connections.app_configured` / `connections.app_removed` as workspace Events (payload: provider, workspace, by, client_id; never the secret).
- Existing `Connection` rows: the migration deletes them (none exist in production; they were minted by the removed site-wide app and cannot refresh without it). Say so in the migration docstring.
- Every route `@gate`; framework layering (connections is L2 features; may import `apps.workspaces`, `apps.agents`, `apps.events`, `apps.common`); regenerate `frontend/src/api/generated.ts`; route docstrings are user-facing (rationale in `#` comments).
- All existing behaviour pinned by `tests/test_connections_*.py`, runner tests and CLI tests keeps working (update tests for renamed things; never weaken an assertion).

## Review Focus

1. A callback for workspace A carrying a state parked for workspace B's app → `bad_state`, nothing exchanged.
2. Admin rotates the secret (PUT with a new secret, same client id) → existing connections keep refreshing with the new secret; PUT with blank secret keeps the stored one.
3. Admin changes the client id → refused (`409 client_id_locked`) while connections exist; remove the app (which disconnects) to switch apps.
4. A person who left the workspace asks for a CLI token → `not_connected`, not the token.
5. Google agent connect signed in as the wrong Google account (not `agent.google_identity()`) → `wrong_account`, nothing stored.

---

### Task 1: ConnectionApp model, per-app providers, services

**Files:** Modify `apps/connections/models.py`, `providers/base.py`, `providers/salesforce.py`, `providers/__init__.py`, `services.py`, `problems.py`; create migration; modify `config/settings/base.py`, `.env.example`; tests `tests/test_connections_*.py`, `tests/fixtures/connections.py`.

**Interfaces — produces:**
- `ConnectionApp(workspace FK CASCADE, provider CharField(40), client_id CharField(255), client_secret_enc TextField, options JSONField(default=dict), scopes CharField(1000, blank) — space-separated extra scopes chosen from the provider's menu, configured_by FK user SET_NULL, created_at, updated_at)`; unique `(workspace, provider)`; property `client_secret` (decrypt) + setter.
- `Connection.app = FK(ConnectionApp, on_delete=PROTECT, related_name="connections")` NOT NULL. (PROTECT: deletion goes through `services.remove_app`, which disconnects first.)
- `Provider.__init__(self, app: ConnectionApp | None = None)`; `client_id`/`client_secret` read the app; `is_configured()` = app present with id+secret; `options` = `app.options`; `requested_scopes()` = `self.scopes + app extra scopes`; `required_scopes` check covers base required + app extra scopes.
- Class attrs added: `scope_choices: Mapping[str, str]` (scope → human label; empty for Salesforce), `option_fields: tuple[OptionField, ...]` where `OptionField(name, label, kind: "url"|"int"|"text", required: bool, default)`, and `clean_options(raw: dict) -> dict` (validates; raises `ValueError(message)`). Salesforce: `login_url` (url, required, https, host is `login.salesforce.com`, `test.salesforce.com` or ends with `.my.salesforce.com`), `access_token_seconds` (int, default 1800, 300–86400). `access_token_lifetime()` reads `options["access_token_seconds"]` else `default_access_token_seconds`. Delete `settings_prefix`/`setting()`.
- `Provider.check_owner(identity, *, owner_kind, owner) -> None` hook (default no-op); raise `ProviderError(code="wrong_account")` to refuse.
- Registry: `providers.CLASSES: dict[str, type[Provider]]`, `providers.get_class(name)`, `providers.for_app(app) -> Provider`. Remove `ALL` singletons (update every user: `title` comes from the class).
- services: `app_for(workspace, provider) -> ConnectionApp` (raises `NotConfigured`); `begin(session, *, app, owner, redirect_uri, return_to="")` parks `{"app": app.pk, "provider", "workspace", ...}`; `complete(session, *, provider, workspace, code, state, redirect_uri, by)` checks parked app's provider+workspace, calls `check_owner` after identity; `Connection` lookup/upsert includes `app`; `access_token_for` / `disconnect` use `providers.for_app(conn.app)`; `connection_for_person(user, provider, *, workspace: str | None = None)` filters to apps in workspaces where the user still has READ, `ambiguous` across several unless `workspace` given; `configure_app(workspace, provider, *, client_id, client_secret, options, scopes, by) -> ConnectionApp` (create or update; blank secret on update keeps the old; client id change with connections → `ConnectionError("client_id_locked")`; validates scopes ⊆ `scope_choices`, options via `clean_options` → `ConnectionError("invalid_app", message)`); `remove_app(app, *, by)`. New codes in `ConnectionError` docstring and `problems.problem_for`: `client_id_locked` 409, `invalid_app` 400, `wrong_account` 409.
- `Subject.workspace`: an agent's connection → agent's workspace (unchanged); a person's connection → still a log line (persons' history stays out of workspace events).

- [ ] Step 1: Write failing tests: app CRUD in services (create, rotate secret keeps connections refreshing, blank secret keeps, client-id lock, invalid scope/option, remove disconnects all + records); begin/complete with app (parked app mismatch → bad_state); person membership filter; `for_app` reading app credentials; Salesforce `login_url` validation; `check_owner` refusal stores nothing.
- [ ] Step 2: Run them; they fail.
- [ ] Step 3: Implement models + migration (schema + delete existing connections), providers, services, problems; remove the settings and `.env.example` lines; update fixtures so existing tests build an app.
- [ ] Step 4: `uv run python -m pytest -q --nomigrations -n auto tests/test_connections_*.py`; `uv run python manage.py makemigrations --check --dry-run`; ruff. All pass.
- [ ] Step 5: Commit.

### Task 2: Routes — per-workspace browser legs, workspace app admin API, token routes

**Files:** Modify `apps/connections/views.py`, `config/urls.py`, `apps/connections/api.py`, `schemas.py`, `apps/api/api.py`, `apps/mcp/api_tools.py` (if new routes need classifying), tests, `frontend/src/api/generated.ts`.

**Interfaces — produces (consumed by Task 4):**
- Views: `connections_start(request, provider, workspace)` — person: requires READ on workspace; agent (`agent=<slug>`, POST only): agent must belong to `workspace` and caller `is_admin`. `connections_callback(request, provider, workspace)`. Redirect URI = `reverse("connections_callback", args=[provider, workspace])`, absolute.
- `GET /api/workspaces/{workspace}/connection-apps` → `ConnectionAppsOut{apps: [ConnectionAppOut]}` with one row per registered provider: `provider, title, configured: bool, client_id, has_secret, options: dict, scopes: list[str], scope_choices: [{scope,label}], option_fields: [{name,label,kind,required,default}], callback_url (absolute), connection_count, updated_at, configured_by_email`. Gate `integrations`.
- `PUT /api/workspaces/{workspace}/connection-apps/{provider}` body `ConnectionAppIn{client_id, client_secret: str = "", options: dict = {}, scopes: list[str] = []}` → `ConnectionAppOut`. `@gate("integrations", setting=True, step_up=True)`.
- `DELETE /api/workspaces/{workspace}/connection-apps/{provider}` → 204, same gate.
- `GET /api/connections/` → `ConnectionsOut{providers: [ProviderOut{name,title,workspace,workspace_name,configured}], connections: [...]}` — providers = one row per (workspace I'm a member of that has a configured app, provider); `ConnectionOut` gains `workspace` and `workspace_name`.
- `GET /api/agents/{slug}/connections` → providers = the agent's workspace's apps (configured or not, with `configured`), plus `ConnectionOut.workspace`.
- `POST /api/me/connections/{provider}/token?workspace=<slug>` (optional); ambiguous → 409 with `workspaces: [slug]`; `connect_url` = absolute start URL for the connection's workspace (or the only configured one).
- Runner turn route: unchanged contract.
- [ ] Steps: failing tests (gates incl. non-admin 403, step-up required from browser, secret never in any response body, callback workspace mismatch, person non-member start → 403/404, agent in another workspace → 404) → implement → regenerate generated.ts (commands in `.superpowers/sdd/2026-10-28-connections/context.md`) → full connections + gate/architecture/MCP-tool tests → commit.

### Task 3: Google provider

**Files:** Create `apps/connections/providers/google.py`; register in `providers/__init__.py`; tests `tests/test_connections_google.py`.

**Interfaces:** `GoogleProvider(Provider)`: `name="google"`, `title="Google"`, authorize `https://accounts.google.com/o/oauth2/v2/auth`, token `https://oauth2.googleapis.com/token`, revoke `https://oauth2.googleapis.com/revoke`, `scopes=("openid", "https://www.googleapis.com/auth/userinfo.email")`, `required_scopes` = those two; `scope_choices` = gmail.readonly "Read mail", gmail.compose "Write drafts", gmail.send "Send mail", gmail.modify "Label and archive mail", calendar.readonly "Read calendars", calendar.events "Edit calendar events" (full `https://www.googleapis.com/auth/...` URLs); `option_fields` = `allowed_domains` (text, optional, comma-separated, e.g. `dimagi.com, dimagi-ai.com`; EMPTY = any Google account, including personal @gmail.com and other organisations' domains — this is the default and must work); `authorize_params`: `access_type=offline`, `prompt=consent` (agent: `prompt=consent select_account`, `login_hint=agent.google_identity()` when set), `hd=<domain>` only when exactly one domain is allowed; identity via `GET https://openidconnect.googleapis.com/v1/userinfo` with the bearer → `account_id=sub`, `label=email`, refuse unverified email; `check_owner`: allowed_domains set and the email's domain is not in it → `wrong_account` (test: empty allowed_domains accepts `someone@gmail.com` and `x@marshellis.com`); agent with `google_identity()` and email differs (case-insensitive) → `wrong_account`. Not rotating; `expires_in` from response.
- [ ] Steps: failing tests at the `requests` boundary (authorize URL params person vs agent, hd, exchange+identity, missing chosen scope → insufficient_scope, wrong agent account, wrong domain, unverified email, revoke) → implement → run → commit. Update the providers package recipe docstring (no settings step; workspace app; callback per workspace).

### Task 4: Frontend

**Files:** Create `frontend/src/components/settings/ConnectionAppsPanel.tsx` (+ test), mount it on the workspace settings page beside the other integrations (find where the Slack/mail workspace integration panels mount); modify `ConnectionsPanel.tsx`, `AgentConnectionsPanel` (+ tests), `frontend/src/api/*` helpers.

- ConnectionAppsPanel (workspace admins): per provider — status, the callback URL with a copy button ("Register this as the callback URL in your Salesforce External Client App / Google OAuth client"), client id, secret field (write-only, placeholder "•••• saved" when `has_secret`), provider option fields rendered from `option_fields`, scope checkboxes from `scope_choices`, Save (handles the step-up prompt the way other step-up writes in settings do), Remove (confirm states it disconnects N connections).
- ConnectionsPanel (person): connect buttons per (workspace, provider) labelled "Connect Google in Dimagi"; when only one workspace has the provider omit the workspace name; connections show their workspace; start URL `/auth/connections/<provider>/<workspace>/start/`.
- AgentConnectionsPanel: start form posts to the agent's workspace start URL; not-configured → "Not set up for this workspace" + link to workspace settings for admins.
- [ ] Steps: failing vitest → implement → `npx vitest run src/components/settings src/pages/agents` + `npm run build` → commit.

### Task 5: Docs + canopy CLI

**Files (canopy-web):** `docs/architecture/vault.md`, `api-surface.md`, `deploy.md` (replace "Connection provider settings" with: nothing to deploy; configured per workspace in Settings; register the per-workspace callback), `tenancy.md`, `CLAUDE.md` if it names the settings, `apps/connections/providers/__init__.py` recipe.
**Files (canopy repo, `~/emdash/worktrees/canopy-cred-salesforce`, new branch from origin/main `feat/connection-token-workspace`):** `src/orchestrator/cred_cli.py` — `connection-token` human mode takes `--workspace <slug>` and sends `?workspace=`; on 409 `ambiguous` print the workspaces and the flag to use; tests in `tests/test_cred_cli.py`.
- [ ] Steps: docs edits; CLI failing test → implement → `uv run --extra browser pytest tests/test_cred_cli.py -q` → commit in each repo.
