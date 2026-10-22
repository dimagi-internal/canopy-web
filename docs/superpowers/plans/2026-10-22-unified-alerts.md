# Unified Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One shared, auditable alert layer that points at first-class records, proposes per-viewer actions that say whether they close the alert, and is rendered by one compact component on Home, Topology, the agent page and the runner detail.

**Architecture:** `Alert` + `AlertEvent` models in `apps/attention`. Each v1 source lives in the app that owns its subject (`apps/harness/alerts_*.py`) and implements `reconcile()` (the full set of currently-true conditions) + `actions(alert, user)`. One off-request tick reconciles every source, expires stale alerts and sends notifications. A user-level REST view (`/api/me/alerts/`) serves screens and MCP.

**Tech Stack:** Django 6 + Django Ninja, PostgreSQL, pytest; React 19 + Tailwind 4, vitest.

**Spec:** `docs/superpowers/specs/2026-10-22-unified-alerts-design.md`

## Global Constraints

- `Alert.workspace` is a NOT NULL FK; never NULL-means-allow. Visibility = member of the alert workspace's organisation tree AND ≥1 action offered (`wsvc.user_workspace_slugs`).
- Severities exactly: `blocking`, `action`, `setup` (ordered in that order).
- Statuses exactly: `open`, `resolved`, `dismissed`; resolve reasons `source`, `action`, `stale`.
- AlertEvent kinds exactly: `raised`, `changed`, `acted`, `notified`, `dismissed`, `undismissed`, `resolved`, `reopened`.
- Titles use the names people gave things (a token's `claude_token_label` / login account), never a slot name like `subscription-1`.
- Every route has `@gate(...)` directly above its `def`; changed `schemas.py`/`api.py` → `npm run gen:api` and commit `generated.ts`.
- Framework apps never import product apps; `attention` imports sources by dotted path only.
- `AlertEvent` is never purged by retention.
- Notifications: only `blocking`, on raise and on reopen, once per alert (`notified_at`), email + Web Push to users holding a *closing* action.
- `turn_failure` raises only for investigations in `escalated`, `debugger_failed`, `held` — never `open`.
- `agent_cant_run` is configuration only (no routes, or no routed runner declares the repo) — never liveness.
- UI compact: one line per alert (dot · source chip · title · inline actions · ×).

## Review Focus

- A source raising the same subject on every tick must not write an event each time — only when `fingerprint` changes (test in Task 2).
- A dismissed alert whose fingerprint is unchanged must stay dismissed across reconciles; a changed fingerprint reopens it (Task 2).
- `act` on an action no longer offered to the caller (lost permission, alert already closed) → 409, nothing logged as acted (Task 4).
- A source that raises (exception) during reconcile must not resolve its open alerts as "gone" (Task 3).
- A person in another organisation never sees an alert even if a source would offer them an action (Task 4).

---

### Task 1: Alert and AlertEvent models

**Files:**
- Modify: `apps/attention/models.py`
- Create: `apps/attention/migrations/000N_alert.py` (makemigrations)
- Modify: `apps/retention/` exclusion if retention enumerates models (check `apps/retention/` for a registry; add AlertEvent as never-purged if so)
- Test: `tests/test_alerts_models.py`

**Interfaces:**
- Produces: `Alert` (fields per spec: `kind` CharField(60), `subject_key` CharField(300), `workspace` FK workspaces.Workspace NOT NULL, `scopes` JSON list, `severity`, `title` (300), `body` Text, `url` (500), `fingerprint` (64), `status`, `resolved_reason`, `closed_by` FK user null, `closed_at`, `first_seen_at`, `last_seen_at`, `occurrences` int, `notified_at` null); unique `(kind, subject_key)`; index `(status, workspace)`. Constants `Alert.BLOCKING/ACTION/SETUP`, `Alert.SEVERITY_RANK = {"blocking":0,"action":1,"setup":2}`, `Alert.OPEN/RESOLVED/DISMISSED`.
- Produces: `AlertEvent(alert FK cascade, at, kind, actor FK user null, action_id CharField blank, payload JSON)`, ordering `at`.

- [ ] **Step 1: Write failing test** `test_alert_unique_per_kind_and_subject` — creating two `Alert`s with the same `(kind, subject_key)` raises `IntegrityError`; `test_alert_event_ordering` — events come back in `at` order.
- [ ] **Step 2: Run** `uv run pytest tests/test_alerts_models.py -v` → FAIL (no model).
- [ ] **Step 3: Add the models; `uv run python manage.py makemigrations attention`.**
- [ ] **Step 4: Run** the test → PASS.
- [ ] **Step 5: Commit** `feat(alerts): Alert and AlertEvent models`.

### Task 2: Alert core service (raise / reconcile / dismiss / act)

**Files:**
- Create: `apps/attention/alerts.py` (types + registry + core service)
- Test: `tests/test_alerts_core.py`

**Interfaces:**
- Produces (dataclasses, frozen):
  - `Raise(subject_key: str, workspace: str, title: str, url: str, scopes: tuple[str, ...], body: str = "", fingerprint: str = "")` — empty fingerprint = hash of title+body.
  - `Action(id: str, label: str, closes: bool, href: str = "", notify: tuple[int, ...] = ())` — `href` = link action; `notify` = user ids for a notify action; neither = server handler.
  - Source protocol (module attributes): `KIND: str`, `SEVERITY: str`, `CONFIRM_WITHIN: timedelta | None`, `CLOSING_ACTION_IDS: tuple[str, ...]` (non-empty), `reconcile(now) -> list[Raise]`, `actions(alert, user) -> list[Action]`, `perform(alert, user, action_id) -> None` (for handler actions).
- Produces: `SOURCES: list[str]` (dotted paths; empty in this task), `source_for(kind) -> module`.
- Produces: `apply_reconcile(kind: str, raises: list[Raise], now) -> None` — upsert each; new → `raised` event; existing open with changed fingerprint → update + `changed` event; dismissed with changed fingerprint → `reopened` (clear `notified_at`); unchanged → bump `last_seen_at` only, no event; open alerts of `kind` not in `raises` → `resolve(alert, reason="source")`.
- Produces: `resolve(alert, *, reason: str, by=None) -> None`, `dismiss(alert, user) -> None`, `undismiss(alert, user) -> None` (each writes its AlertEvent in the same transaction).
- Produces: `visible_actions(alert, user) -> list[Action]` — `source.actions(alert, user)` plus framework `Action("dismiss", "Dismiss", closes=True)` when the source lists `"dismiss"` in the returned ids (sources opt in by returning it; the framework handles it).
- Produces: `act(alert, user, action_id: str) -> Alert` — raises `ActionNotOffered` if `action_id` not in `visible_actions`; logs `acted`; `dismiss` → `dismiss()`; notify → `notify_users(alert, users, by=user)` (Task 3) and alert stays open; handler → `source.perform` then if `closes` → `resolve(reason="action", by=user)`; link actions are not acted server-side.

- [ ] **Step 1: Write failing tests** with a fake source module registered for the test (monkeypatch `SOURCES`):
  - `test_reconcile_raises_once_and_is_quiet_on_repeat` — two `apply_reconcile` with the same Raise → 1 Alert, 1 `raised` event, `last_seen_at` advanced.
  - `test_reconcile_resolves_missing` — second reconcile with `[]` → status `resolved`, reason `source`, `resolved` event.
  - `test_dismiss_sticks_until_fingerprint_changes` — dismiss; reconcile same fingerprint → still dismissed; reconcile new fingerprint → `open` + `reopened` event, `notified_at` None.
  - `test_act_not_offered_raises` — `act(alert, other_user, "fix")` when actions returns [] → `ActionNotOffered`, no `acted` event.
  - `test_closing_handler_resolves_with_actor` — `act(..., "fix")` with `closes=True` → `resolved`, reason `action`, `closed_by` = user.
  - `test_every_registered_source_declares_a_closing_action` — for each path in `SOURCES`, `CLOSING_ACTION_IDS` non-empty (stays green with the real registry in later tasks).
- [ ] **Step 2: Run** `uv run pytest tests/test_alerts_core.py -v` → FAIL.
- [ ] **Step 3: Implement in `apps/attention/alerts.py`.** Each AlertEvent write also calls `apps.events.services.record([{source:"alert", key:f"{alert.pk}", kind:f"alert.{kind}", summary: alert.title, level: "warning" if blocking else "info"}], workspace=alert.workspace)`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(alerts): core service — reconcile, dismiss, act`.

### Task 3: The tick — reconcile every source, expire stale, notify

**Files:**
- Modify: `apps/attention/alerts.py` (add `tick`, `notify_users`, `notify_new_blocking`)
- Modify: `apps/attention/apps.py` (`ready()` → `ticks.register("alerts", alerts.tick, 60)`)
- Test: `tests/test_alerts_tick.py`

**Interfaces:**
- Consumes: Task 2 service.
- Produces: `tick(now=None) -> None` — for each source: `raises = reconcile(now)` inside try; on exception log and SKIP that source entirely (no resolves); then `apply_reconcile`. Then resolve as `stale` any open alert whose source has `CONFIRM_WITHIN` and `last_seen_at < now - CONFIRM_WITHIN`. Then `notify_new_blocking(now)`.
- Produces: `notify_users(alert, users, *, by=None) -> int` — email (`apps.workspaces.notifications._send` + `_button` to `alert.url`) and `apps.push.services.send_to_user(user, title, body, url)`; writes one `notified` event with `payload={"to":[ids], "by": by id}`.
- Produces: `notify_new_blocking(now)` — open `blocking` alerts with `notified_at` null → users who hold a closing action (iterate the workspace tree's members via `wsvc.member_roles_of`, filter `visible_actions` for any `closes`), `notify_users`, set `notified_at`.

- [ ] **Step 1: Failing tests:**
  - `test_failing_source_resolves_nothing` — fake source whose `reconcile` raises; pre-existing open alert stays `open`.
  - `test_stale_alert_resolves_as_stale` — fake source with `CONFIRM_WITHIN=1h` whose `reconcile` raises; its open alert has `last_seen_at` 2h ago → after `tick`, status `resolved`, reason `stale` (a failing source resolves nothing as "gone", but staleness still applies).
  - `test_blocking_notifies_closers_once` — monkeypatch `send_to_user` and mail backend (`mailoutbox`); two ticks → one mail to the closer, none to a notify-only viewer, `notified_at` set, one `notified` event.
  - `test_action_severity_does_not_notify`.
- [ ] **Step 2: Run** `uv run pytest tests/test_alerts_tick.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; also `uv run pytest tests/test_off_request_ticks.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(alerts): reconcile tick, stale expiry, blocking notifications`.

### Task 4: REST + MCP — `/api/me/alerts/`

**Files:**
- Create: `apps/attention/alerts_api.py`, add schemas to `apps/attention/schemas.py`
- Modify: `apps/api/api.py` (mount router at `/me/alerts/`, next to attention's mount)
- Modify: `frontend/src/api/generated.ts` (regenerate)
- Modify: `docs/architecture/api-surface.md` (routes), `docs/architecture/mcp-surface.md` if it lists tools
- Test: `tests/test_alerts_api.py`

**Interfaces:**
- Produces schemas: `AlertActionOut{id,label,closes,href}`, `AlertOut{id,kind,severity,title,body,url,workspace,organisation,scopes,status,first_seen_at,last_seen_at,occurrences,closed_by_email,closed_at,actions: list[AlertActionOut], last_notify: str}` (`last_notify` = display line of the latest `notified` event, e.g. "Sift's owner asked Jonathan Jackson at 14:02"), `AlertEventOut{at,kind,actor_email,action_id,payload}`, `ActIn{action:str}`.
- Routes (all `@gate("self")` reads / `@gate("self", kind="action")` writes; operationIds = function names):
  - `GET /` `list_alerts(scope: str | None = None, status: Literal["open","dismissed"]="open")` → `list[AlertOut]`, read per organisation via `wsvc.each_organisation`; only alerts in `wsvc.request_workspace_slugs(request)`; drop rows with no actions (for `dismissed`: rows where the source would offer `dismiss`, last 7 days); `scope` matches an entry of `scopes`; ordered by `SEVERITY_RANK` then `-last_seen_at`.
  - `POST /{id}/act` `act_on_alert(payload: ActIn)` → `AlertOut`; 404 if not visible; 409 `action-not-offered` on `ActionNotOffered`.
  - `POST /{id}/undismiss` `undismiss_alert` → `AlertOut`; 409 unless caller is offered `dismiss`.
  - `GET /{id}/history` `alert_history` → `list[AlertEventOut]`; 404 unless visible.

- [ ] **Step 1: Failing tests:** `test_list_shows_only_alerts_with_actions`, `test_list_excludes_other_organisation` (user in org B, source offers them an action, alert in org A → absent), `test_scope_filter`, `test_act_dismiss_clears_for_everyone` (two closers; one dismisses; the other's list is empty, dismissed list shows `closed_by_email`), `test_act_not_offered_409`, `test_history_visible_to_viewer_404_to_stranger`.
- [ ] **Step 2: Run** `uv run pytest tests/test_alerts_api.py -v` → FAIL.
- [ ] **Step 3: Implement; then** `cd frontend && npm run gen:api:local`.
- [ ] **Step 4: Run** the file plus `uv run pytest tests/test_every_route_declares_its_gate.py tests/test_architecture_boundary.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(alerts): /api/me/alerts/ list, act, undismiss, history`.

### Task 5: Source `runner_credential`

**Files:**
- Create: `apps/harness/alerts_runner_credential.py`
- Modify: `apps/attention/alerts.py` `SOURCES` (append dotted path)
- Modify: `apps/events/api.py:89` / `apps/harness/credential_notify.py` — stop mailing `claude_auth_required` (the alert's notification replaces it); keep `claude_usage_exhausted` and `claude_api_key_fallback` mail unchanged
- Test: `tests/test_alerts_runner_credential.py`

**Interfaces:**
- `KIND="runner_credential"`, `SEVERITY="blocking"`, `CONFIRM_WITHIN=timedelta(hours=2)`, `CLOSING_ACTION_IDS=("set_token","dismiss")`.
- `reconcile(now)`: non-retired runners with `workspace_id` set; for each check in `runner.health["checks"]` named `claude.probe.<slot>` with `status=="fail"` and detail containing "not signed in" → `Raise(subject_key=f"runner:{runner.pk}:cred:{slot}", workspace=runner.workspace_id, title=f'The "{label}" token on {runner.name} is signed out', url=<credential_notify.runner_url(runner) made workspace-relative>, scopes=(f"runner:{runner.pk}", f"workspace:{runner.workspace_id}", *[f"agent:{s}" for s in agents routed to runner]), body=check detail, fingerprint=slot+detail-without-timestamp)`.
- `label_for(runner, slot) -> str`: `subscription-1` → `runner credential.claude_token_label or "primary token"`; `subscription-2` → `claude_token_secondary_label or "secondary token"`; `box-login:<key>` → that login's account email; else slot. (Find the credential row the same way `get_runner_credential_status` does.)
- `actions(alert, user)`: `can_administer_runner(user, runner)` → `[Action("set_token", f'Set a new "{label}" token', True, href=runner_url), dismiss]`; else owner of an agent in `scopes` (and not the runner owner) → `[Action("ask_owner", f"Ask {owner name} to fix it", False, notify=(runner.owner_id,))]`; else `[]`.
- Runners with `workspace_id` NULL (private runners) are skipped in v1 — note it in the module docstring.

- [ ] **Step 1: Failing tests:** `test_signed_out_probe_raises_with_label` (health check `claude.probe.subscription-1` fail, `claude_token_label="hal"` → title contains `"hal"` and not `subscription-1`), `test_probe_passing_resolves`, `test_admin_gets_set_token_and_dismiss`, `test_agent_owner_gets_notify_only`, `test_auth_required_event_no_longer_mails`.
- [ ] **Step 2: Run** `uv run pytest tests/test_alerts_runner_credential.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** it plus `uv run pytest tests/ -q -k "credential_notify or events"` → PASS (update any test that asserted the removed mail).
- [ ] **Step 5: Commit** `feat(alerts): runner credential source names tokens by their label`.

### Task 6: Source `turn_failure`

**Files:**
- Create: `apps/harness/alerts_turn_failure.py`; append to `SOURCES`
- Test: `tests/test_alerts_turn_failure.py`

**Interfaces:**
- `KIND="turn_failure"`, `SEVERITY="action"`, `CONFIRM_WITHIN=None`, `CLOSING_ACTION_IDS=("mark_resolved","dismiss")`.
- `reconcile`: `FailureInvestigation` with status in (`escalated`,`debugger_failed`,`held`) → `Raise(subject_key=f"investigation:{inv.pk}", workspace=inv.workspace_id, title=<"Turns failing on {runners}: {sample_note first sentence, ≤120 chars}">, scopes=(workspace, *runner scopes by name lookup, *agent scopes), fingerprint=f"{inv.status}:{inv.occurrences}")`.
- `actions`: `auto_debug.can_handle(user, alert.workspace_id)` → `[Action("open", "Open the investigation", False, href=<investigation page or turn page>), Action("mark_resolved","Mark resolved",True), dismiss]`; else `[]`.
- `perform(alert, user, "mark_resolved")` → `auto_debug.resolve(inv, note="Resolved from the alert", by=user)`.

- [ ] **Step 1: Failing tests:** `test_open_investigation_raises_nothing`, `test_escalated_raises_action_alert`, `test_mark_resolved_resolves_investigation_and_alert`, `test_non_log_reader_sees_nothing`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(alerts): failed turns Ada could not handle become alerts`.

### Task 7: Sources `unclaimable_turns` and `device_key_pending`

**Files:**
- Create: `apps/harness/alerts_unclaimable.py`, `apps/harness/alerts_device_key.py`; append both to `SOURCES`
- Modify: `apps/harness/device_key_notify.py` — `notify_pending_on_commit` no longer mails (alert notification replaces it); invitation and rekeyed notices unchanged
- Test: `tests/test_alerts_unclaimable.py`, `tests/test_alerts_device_key.py`

**Interfaces:**
- `unclaimable_turns`: `SEVERITY="blocking"`, `CONFIRM_WITHIN=timedelta(minutes=30)`, `CLOSING_ACTION_IDS=("cancel","dismiss")`. `reconcile`: `claim.unclaimable_queued_turns(ws_slugs=<all workspace slugs>)` filtered to `kind=="config"` (offline is transient — raise bar), grouped by agent → one Raise per agent `subject_key=f"agent:{slug}:unclaimable"`, title `"{n} queued turns for {Agent name} have no runner that can take them"`, fingerprint = sorted turn ids. `actions`: agent admins / the turns' requesters with cancel rights (`can_cancel` as `list_unclaimable_turns` computes it) → `[Action("cancel","Cancel these turns",True), Action("route","Fix routing",False, href=agent routing settings), dismiss]`. `perform("cancel")` cancels each turn via the same service `cancelTurn` uses.
- `device_key_pending`: `SEVERITY="blocking"`, `CONFIRM_WITHIN=None`, `CLOSING_ACTION_IDS=("approve","dismiss")`. `reconcile`: `RunnerDeviceKey` with status `pending` → `subject_key=f"device_key:{key.pk}"`, workspace = runner.workspace_id (skip NULL), title `"A new device key on {runner.name} is waiting for approval"`, url = the existing approve deep link from `device_key_notify`. `actions`: `may_trust_device_keys(user, runner)` → `[Action("approve","Approve key",True, href=link), dismiss]`.

- [ ] **Step 1: Failing tests:** `test_config_stuck_turns_raise_one_alert_per_agent`, `test_offline_stuck_turns_raise_nothing`, `test_cancel_action_cancels_and_resolves`, `test_pending_key_raises_and_approval_resolves`, `test_pending_key_no_longer_mails_directly`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** these plus `uv run pytest tests/ -q -k "device_key or unclaimable"` → PASS.
- [ ] **Step 5: Commit** `feat(alerts): unclaimable turns and pending device keys`.

### Task 8: Source `agent_cant_run`

**Files:**
- Create: `apps/harness/alerts_agent_routing.py`; append to `SOURCES`
- Test: `tests/test_alerts_agent_routing.py`

**Interfaces:**
- `KIND="agent_cant_run"`, `SEVERITY="setup"`, `CONFIRM_WITHIN=None`, `CLOSING_ACTION_IDS=("dismiss",)` plus a link action.
- `reconcile`: for each root workspace, `topology.build(root)`; for each agent whose effective order (own or inherited) has no runners → Raise `"{name} has no runner to run on"`; for each agent whose `follows.missing_repo` is non-empty and no routed runner declares its repo → Raise `"{name}: {runners} don't have its repo"`. `subject_key=f"agent:{slug}:routing"`, fingerprint = the computed reason. Liveness (`health` down/stale) is ignored.
- `actions`: agent admins (`apps.agents.access`/agent admin check used by routing routes) → `[Action("routing","Fix routing",False, href=agent routing page), dismiss]`. (Routing fixed → reconcile resolves it; that is the close path, so the source's closing ids are `dismiss`.)

- [ ] **Step 1: Failing tests:** `test_agent_with_no_route_raises_setup`, `test_offline_runner_does_not_raise`, `test_missing_repo_raises_and_fix_resolves`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(alerts): agents that can't run (configuration only)`.

### Task 9: `<AlertList>` and the four screens

**Files:**
- Create: `frontend/src/api/alerts.ts` (`loadAlerts(scope?, status?)`, `actOnAlert(id, action)`, `undismissAlert(id)`)
- Create: `frontend/src/components/alerts/AlertList.tsx`, `frontend/src/components/alerts/AlertList.test.tsx`
- Modify: `frontend/src/pages/home/HomePage.tsx` (alerts section at top + "Dismissed (n)" fold)
- Modify: `frontend/src/pages/topology/TopologyMapPage.tsx` (replace "Agents whose turns can't run" section, the "can't run" badge and the inline "lacks its repo" line with `<AlertList scope="workspace:<slug>">`; keep the other two lists)
- Modify: agent workspace layout (the component routed at `/w/:workspace/agents/:slug`, `frontend/src/router.tsx:393`) — `<AlertList scope="agent:<slug>" hideEmpty>` under the header
- Modify: `frontend/src/components/supervisor/RunnerDetail.tsx` — `<AlertList scope="runner:<id>" hideEmpty>` above `RunnerHealth`
- Modify: `docs/architecture/pages.md` (Home, Topology, agent, runner detail mention alerts); add `docs/architecture/alerts.md` (the model, sources, rules against a wasteland) and link it from CLAUDE.md's topic index

**Interfaces:**
- `AlertList({ scope?: string, hideEmpty?: boolean, showCounts?: boolean, showDismissed?: boolean })`. Polls with `usePolled` like Home. Row: severity dot (blocking red / action orange / setup grey), source chip (kind → "Runner credential", "Failed turns", "Stuck turns", "Device key", "Agent routing"), title, `last_notify` line when present, actions as buttons (`href` → `<a>`; else `actOnAlert`), each with a small "closes"/"stays open" tag, `dismiss` rendered as an icon-only × (`aria-label="Dismiss for everyone"`). `showCounts` renders severity chips filtering the list. `showDismissed` renders the collapsed fold with `closed_by_email`, `closed_at` and Undo.

- [ ] **Step 1: Failing vitest tests:** `renders one line per alert with closes tags`, `dismiss removes the row and calls actOnAlert with "dismiss"`, `href actions render as links`, `hideEmpty renders nothing for no alerts`.
- [ ] **Step 2: Run** `cd frontend && npx vitest run src/components/alerts` → FAIL.
- [ ] **Step 3: Implement the component, then wire the four screens.**
- [ ] **Step 4: Run** `cd frontend && npx vitest run && npm run build` → PASS; `uv run pytest tests/test_claude_md_budget.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(alerts): AlertList on Home, Topology, agent and runner pages`.

### Task 10: Ship

- [ ] **Step 1:** `uv run pytest -q` → all pass; `cd frontend && npm run build` → ok.
- [ ] **Step 2:** Open PR from `ada/unified-alerts` (body: summary, spec link, mockup link, what replaced which surface); `gh pr merge <n> --auto`; confirm `autoMergeRequest` set.
- [ ] **Step 3:** After merge: `gh workflow run "Deploy to Labs (AWS)" --ref main`; confirm the deployed sha includes the merge and `/api/openapi.json` lists `list_alerts`.
- [ ] **Step 4:** Live check: cloud-ec2-2's "hal" token alert appears on Home and on cloud-ec2-2's runner detail, titled with "hal".
