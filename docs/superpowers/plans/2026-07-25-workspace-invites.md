# Workspace Invites — a real way into a canopy workspace

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make invites the supported way into a canopy workspace, so auto-join-by-email-domain can be restricted to `dimagi` alone. An owner invites someone (any email, including external), shares a link, the invitee signs in and lands as a member with the invited role.

**Why now:** ace-web's chat needs its users in the `connect` workspace. Opening `connect` to a whole email domain would hand every Dimagi user editor access to the entire agent fleet's sessions, items and runs. Invites are the narrow alternative — but today they are unusable: no UI, no accept page, and the OAuth adapter rejects a non-`dimagi.com` invitee *before* they can ever reach the accept endpoint.

**Architecture:** The backend CRUD already exists (`apps/workspaces`: `WorkspaceInvite` with token/role/expiry/revoke, owner-gated create/list/revoke, email-matched accept). This plan adds the missing halves: a service layer, an invite-aware login gate, a members/invites admin surface, and an `/invite/:token` accept page.

**Delivery is a copy-link, not an email.** The repo has *zero* email infrastructure (no `EMAIL_BACKEND`, no provider dependency, no templates), and adding SES means a verified domain plus a sandbox-exit request — out of band and not something this plan can complete. So the inviter copies a link and sends it however they already talk to that person. `Invite.email` still binds who may accept, so the link is not a bearer credential for anyone who finds it. Email delivery can be layered on later without changing the model, the API, or the UI.

**Tech Stack:** Django 5 + django-ninja + allauth; React 19 + Vite + Tailwind 4 + `canopy-ui`; pytest; vitest.

## Global Constraints

- Backend tests `uv run pytest`; frontend `cd frontend && npm run test && npm run build`.
- After any `apps/**/api.py` or `schemas.py` change: regenerate `frontend/src/api/generated.ts` locally (Django-shell schema dump → `npm run gen:api:local`) and commit it. CI verifies freshness; the workflow's auto-commit can't re-trigger required checks.
- Non-member access to tenant resources returns **404, never 403**.
- Semantic design tokens only — no raw Tailwind palette literals.
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- One branch: `feat/workspace-invites` off `origin/main`. PR at the end.

---

### Task 1: Service layer + invite hardening

**Files:** `apps/workspaces/services.py`, `apps/workspaces/api.py`, `apps/workspaces/models.py` (+ migration), `apps/workspaces/tests/test_invites.py` (create)

**Interfaces (later tasks call these by name):**
```python
create_invite(*, workspace, email, role, invited_by) -> WorkspaceInvite   # reuses a live pending invite for the same (workspace, email) instead of duplicating
accept_invite(*, token, user) -> tuple[Workspace, str]                     # returns (workspace, role); raises InviteError(code) on invalid/expired/revoked/email-mismatch
revoke_invite(*, invite) -> None                                           # idempotent
pending_invite_for_email(email) -> WorkspaceInvite | None                  # used by the login gate in Task 2
class InviteError(Exception):  # .code in {"not_found","expired","revoked","already_accepted","email_mismatch"}
```

- [ ] **Step 1 — failing tests** (`tests/test_invites.py`): create-then-accept sets membership at the invited role; a second `create_invite` for the same (workspace, email) returns the SAME row rather than a duplicate; accepting an expired invite raises `InviteError("expired")`; a revoked one raises `revoked`; a wrong-email user raises `email_mismatch`; accepting when already a member at a different role keeps the existing role and still marks the invite accepted (document that choice in the test name); `pending_invite_for_email` ignores expired/revoked/accepted rows and is case-insensitive.
- [ ] **Step 2** — run, confirm failures.
- [ ] **Step 3** — implement the four functions in `services.py`, moving the logic out of `api.py`; rewrite the three invite views to call them and map `InviteError.code` → HTTP (`not_found`→404, `expired`/`revoked`/`already_accepted`→410, `email_mismatch`→403), preserving today's status codes so the existing `tests/test_members.py` still passes. Normalize `email` to lowercase on create.
- [ ] **Step 4** — add a partial unique constraint so one workspace cannot accumulate duplicate live invites for an address: unique on `(workspace, email)` `WHERE accepted_at IS NULL AND revoked_at IS NULL`, plus its migration. (`create_invite` already avoids tripping it; the constraint is the backstop.)
- [ ] **Step 5** — `uv run pytest apps/workspaces -v`, then the full suite; commit.

### Task 2: Invite-aware login gate (the blocker)

**Files:** `apps/common/auth_adapter.py`, `apps/common/tests/test_auth_domains.py` (extend) or a new test module

Today `CustomSocialAccountAdapter.pre_social_login` rejects any email outside `AUTH_ALLOWED_EMAIL_DOMAIN` with the 403 `domain_rejected.html` page — globally, before workspace context exists. So an external invitee can never sign in to accept. That makes invites a Dimagi-only feature and leaves domain auto-join as the only real way in, which is exactly what we are trying to stop relying on.

- [ ] **Step 1 — failing tests:** a login for an email outside the allowlist that has a **pending** invite is admitted; the same email with only an expired/revoked/accepted invite is still rejected; an allowlisted-domain login is unaffected; the invite is NOT consumed by logging in (acceptance still requires the accept call).
- [ ] **Step 2** — run, confirm failures.
- [ ] **Step 3** — implement: in `pre_social_login`, when the domain check fails, admit the login iff `services.pending_invite_for_email(email)` returns a row. Keep the existing `_connect_jit_identity` behavior. Comment the security reasoning: the invite is an explicit, revocable, expiring, per-address grant issued by a workspace owner — a strictly narrower admission than adding a domain to the global allowlist, and it still grants nothing until the invite is accepted.
- [ ] **Step 4** — full suite; commit.

### Task 3: Invite preview endpoint (pre-auth, minimal disclosure)

**Files:** `apps/workspaces/api.py`, `apps/workspaces/schemas.py`, `apps/common/middleware.py`, tests

The accept page must tell an unauthenticated visitor what they were invited to, before sending them through Google.

- [ ] **Step 1 — failing tests:** `GET /api/workspaces/invites/{token}/preview` with `auth=None` returns `{workspace_slug, workspace_display_name, role, email_hint, status}` for a pending invite; a revoked/expired/accepted token returns the same shape with the right `status` and **no** workspace details; an unknown token 404s. `email_hint` must be masked (e.g. `j•••@dimagi.com`) — the token may be forwarded, and the full invitee address is not the finder's business.
- [ ] **Step 2** — run, confirm failures.
- [ ] **Step 3** — implement the endpoint and add `/api/workspaces/invites/` to the login middleware's public prefixes (an explicit allowlist entry, in the same style as `/share/` and `/review/` — not an accident of the catch-all).
- [ ] **Step 4** — regenerate `generated.ts`; full suite; commit.

### Task 4: Members + invites admin surface

**Files:** `frontend/src/api/workspaces.ts`, `frontend/src/pages/WorkspaceMembersPage.tsx` (create), `frontend/src/router.tsx`, a link from the workspace switcher or app nav, tests

- [ ] **Step 1** — extend the API client: `listMembers`, `removeMember`, `listInvites`, `createInvite`, `revokeInvite` (typed off `generated.ts`, no `as never` casts).
- [ ] **Step 2 — failing component tests:** the page lists members with roles and pending invites; an owner sees the invite form and revoke/remove controls, a non-owner sees neither; creating an invite renders the resulting link with a **copy** button and an explicit "send this to them yourself — canopy does not email it" note; revoke removes the row.
- [ ] **Step 3** — build the page at route `/w/:workspace/members`, following the existing tenant-scoped route pattern; render through the shared kit primitives and semantic tokens. Handle the API's 404-for-non-member by redirecting to the workspace home.
- [ ] **Step 4** — `npm run test && npm run build`; commit.

### Task 5: `/invite/:token` accept page

**Files:** `frontend/src/pages/InviteAcceptPage.tsx` (create), `frontend/src/router.tsx`, tests

- [ ] **Step 1 — failing tests:** unauthenticated + pending → shows workspace name/role and a sign-in action that returns to this URL; authenticated + pending → shows an Accept button that calls the accept endpoint and then navigates into the workspace; expired/revoked/accepted → a clear terminal message, no Accept button; email mismatch (403 from accept) → explains that the invite is bound to a different address and offers to sign out.
- [ ] **Step 2** — run, confirm failures.
- [ ] **Step 3** — implement, reading state from the Task 3 preview endpoint, then calling accept. Register the route outside the `/w/:workspace/` tree.
- [ ] **Step 4** — `npm run test && npm run build`; commit.

### Task 6: Restrict auto-join to `dimagi`, and say so

**Files:** `apps/workspaces/services.py`, `apps/workspaces/management/commands/audit_auto_join.py` (create), `CLAUDE.md`, tests

- [ ] **Step 1 — failing tests:** `ensure_default_workspace()` still seeds `dimagi` with the allowed domains; creating any other workspace leaves `auto_join_domains` empty; a test asserts that no workspace other than `dimagi` may be seeded with auto-join domains by our own code paths.
- [ ] **Step 2** — run, confirm failures.
- [ ] **Step 3** — implement: ensure `create_workspace` never copies domains in; add the `audit_auto_join` management command that prints every workspace with non-empty `auto_join_domains` and, with `--fix`, clears them for every workspace except `dimagi` (so the production posture is checkable and repairable, not folklore).
- [ ] **Step 4** — CLAUDE.md: document the rule — **`dimagi` is the only auto-join workspace; every other workspace is invite-only** — and point at `/w/:workspace/members` for the invite flow and at the command for auditing.
- [ ] **Step 5** — full suite; commit; open the PR.

---

## Self-review notes

- The report's items 1-7 map to Tasks 2, (delivery decision, recorded above), 4, 5, 3, 4, 1 respectively; item 8's hardening is Task 1 Steps 1/4.
- Deliberately out of scope: sending email (no infrastructure; the model/API/UI do not change when it is added later), per-invite workspace *creation*, and bulk invites.
- The `email_hint` masking in Task 3 is the one place where a token grants any information at all; everything else requires an authenticated, email-matched user.
