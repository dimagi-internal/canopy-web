# Tenant-scoped user provisioning for embedding apps

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** An authorized embedding app (ace-web today) can bring its own users into **one specific canopy workspace** — the tenant it was granted — as a first-class, server-authoritative capability. No domain-wide auto-join, no human issuing invites for machine-provisioned accounts.

## Why

Today token exchange JIT-creates the Django `User` but grants **no membership**, so an ace-web user hits 404 on their first session create. The only ways in are (a) adding a whole email domain to `Workspace.auto_join_domains` — which would hand every Dimagi user editor access to `connect`, where the entire agent fleet's sessions, items and runs live — or (b) a human hand-issuing an invite per user, which is not a flow for a machine-driven product. The manual membership row currently in production for `ace@dimagi-ai.com` is a placeholder for exactly this gap.

The grant belongs on the **credential**, not in the embedder's config. Today ace-web sends `CANOPY_WORKSPACE=connect` from its own environment; the server trusts the URL and checks membership. If the credential carries its tenant, canopy becomes authoritative: a compromised or misconfigured ace-web cannot provision into a workspace it was never granted, because the workspace is never client input.

## Design

Add to `AppCredential`:
- `provision_workspace` — FK → `Workspace`, **nullable**. Null = no provisioning power (today's behavior, preserved for any future read-only embedder).
- `provision_role` — `viewer|editor`, default `editor`. **`owner` is not permitted** — an app must never be able to mint an administrator of a tenant, because owners can invite, remove members, and change roles.

Token exchange gains one step, after the existing domain double-gate and JIT user creation:

```
if credential.provision_workspace_id:
    ensure_member(credential.provision_workspace, user, credential.provision_role)
```

`ensure_member` is already `get_or_create`, so it is **create-only**: an existing member's role is never raised or lowered by an app. Provisioning can add someone to a tenant; it can never escalate someone already in it.

The exchange response gains `workspace` (the provisioned slug, or null), so the embedder can use the server's answer instead of its own config.

**What this does NOT change:** the domain allowlist still applies first (a credential can only ever act for its allowlisted domains); the invite flow is untouched (humans inviting humans); auto-join stays `dimagi`-only; delegated tokens remain short-lived and unscoped-beyond-the-user.

**Blast radius, stated plainly:** a leaked ace-web credential can now add arbitrary allowlisted-domain addresses to *one* workspace as editor — previously it could only mint sessions for users who already had access. That is a real increase, bounded by: one workspace fixed at provisioning time, never `owner`, never an escalation of an existing member, and revocable by revoking the credential. It is strictly narrower than the alternative it replaces (opening `connect` to a whole email domain, which would grant every Dimagi user editor on the agent fleet permanently and irrevocably).

## Global Constraints

- Branch `feat/tenant-provisioning` off `origin/main`. One PR.
- `uv run pytest`; `uv run ruff check . --select F --ignore F403,F405`; regenerate `frontend/src/api/generated.ts` and commit it (CI fails on stale types).
- 404-not-403 for tenant resources. Commit trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Model + provisioning in the exchange

**Files:** `apps/tokens/models.py` (+migration), `apps/tokens/exchange_api.py`, `apps/tokens/schemas.py` if the response schema lives there, `apps/tokens/management/commands/create_app_credential.py`, tests.

**Interfaces:** `AppCredential.create_credential(*, name, domains, created_by, provision_workspace=None, provision_role="editor")`; exchange response gains `workspace: str | None`.

- [ ] **Step 1 — failing tests.** Cover, at minimum:
  - a credential with `provision_workspace` set: exchanging for an allowlisted address makes that user a member of exactly that workspace at `provision_role`, and of no other workspace;
  - a credential with `provision_workspace=None` grants **no** membership (today's behavior — assert the user has zero memberships);
  - an **existing member at a different role is left unchanged** (create-only; assert the role did not move, in both directions);
  - a **disallowed domain still 403s and provisions nothing** — assert no `User` and no membership were created (the domain gate must run before any write);
  - `provision_role="owner"` is rejected at creation (`ValueError`/validation), so no credential can ever mint an owner;
  - the exchange response carries the provisioned `workspace` slug (and `null` when there is none);
  - an **inactive** user still 403s and provisions nothing (the existing `is_active` gate must not be bypassed by the new write).
- [ ] **Step 2** — run, confirm failures.
- [ ] **Step 3** — implement. Put the provisioning call in the exchange **after** every existing gate (credential valid → domain allowed → user active), so a rejected exchange can never leave a membership behind. Use `apps.workspaces.services.ensure_member`. Enforce the no-owner rule in `create_credential` **and** as a model-level guard so a shell caller cannot set it either.
- [ ] **Step 4** — extend `create_app_credential` with `--workspace` and `--role`, validating the workspace exists and the role is not `owner`; print what was granted so the operator sees the tenant in the output.
- [ ] **Step 5** — migration; full suite; ruff; regen types; commit.

### Task 2: Make the credential's tenant authoritative for ace-web

**Files:** ace-web repo — `/Users/acedimagi/emdash/worktrees/ace-web-canopy-cutover` (branch off its `origin/main`).

ace-web currently sends `CANOPY_WORKSPACE` from its own settings. Prefer the server's answer:
- [ ] `/api/canopy/token` already proxies the exchange — surface the returned `workspace` to the SPA via `/api/canopy/status` (or the token response), and use it in place of the configured value when present, falling back to `CANOPY_WORKSPACE` only if the server returns null.
- [ ] Update the session-create call to use it. Tests for both branches.
- [ ] Note in CLAUDE.md that `CANOPY_WORKSPACE` is now a fallback, not the source of truth.

*(This is a separate PR in a separate repo; do Task 1 first and deploy it, since Task 2 depends on the new response field.)*

### Task 3: Docs + the production cutover

- [ ] CLAUDE.md (canopy): document `AppCredential.provision_workspace` under the auth/tenancy section — what it grants, why `owner` is forbidden, and that it is create-only.
- [ ] After deploy: grant the live `ace-web` credential `provision_workspace=connect`, `provision_role=editor` via a one-off task; verify by exchanging for a fresh allowlisted address and confirming it lands in `connect` as editor with no other membership; then **remove the manual membership row** added earlier for `ace@dimagi-ai.com` and confirm it is re-provisioned automatically on next exchange.

## Self-review notes

- The security question this must survive: can an app credential reach a workspace it was not granted? The workspace is a server-side FK on the credential and never appears in the request, so the answer must be structurally no — verify no code path lets a request influence it.
- Deliberately out of scope: multiple workspaces per credential (a through-table if it's ever needed), and letting an app *remove* members (a strictly larger power with no current use).
