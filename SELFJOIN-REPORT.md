# Replace auto-join with explicit self-join — report

## Scope note (discrepancy from the brief)

The brief enumerated **six** call sites of `auto_join_workspaces`. A full-repo grep
at the start of this task found **31 call sites across 19 files** (plus the
function's two internal uses inside `apps/workspaces/services.py` itself, in
`request_workspace_slugs` and `workspace_slugs_for_user_id` — the shared helpers
most of those 19 files were actually calling through). The six-site count in the
brief appears to predate several apps (`harness`, `canopy_sessions`, `timeline`,
`runs`, `tokens`, `shareouts`, `reviews`, `walkthroughs`, `realtime`) adopting the
same auto-join-then-scope pattern. Per Jonathan's own framing at the top of the
brief ("let's actually get rid of the auto-join workspace functionality"), I
removed **every** call site, not just the six listed, and deleted the function
entirely. Full list is in "Call sites removed" below.

## 1. Migration — `RenameField`, data proven to survive

`apps/workspaces/migrations/0008_rename_auto_join_domains_self_join_domains.py`
uses `migrations.RenameField` (never drop-and-recreate) followed by an
`AlterField` that only updates the help text — no `RemoveField`/`AddField` pair
appears anywhere in the migration.

**Proof the data survives** (migrated a scratch sqlite DB from migration 0007 →
0008, inserting a row via raw SQL against the pre-rename schema, then reading it
back through the current ORM model after migrating forward):

```
columns before migration 0008: [..., 'auto_join_domains', ...]
inserted row (pre-migration): [('dimagi', '["dimagi.com", "dimagi-associate.com"]')]
=== migrated to 0008 ===
columns after migration 0008: [..., 'self_join_domains', ...]
ORM read via renamed field self_join_domains: ['dimagi.com', 'dimagi-associate.com']
```

`makemigrations --check --dry-run` reports "No changes detected" against the
renamed model, confirming the migration and model are in sync.

## 2. `auto_join_workspaces` deleted, no stub left

Deleted from `apps/workspaces/services.py`. Replaced by two new functions:
`joinable_workspaces(user)` (capability list) and `join_workspace(user, slug)`
(explicit join via `ensure_member`, raising `JoinError` for both "no such
workspace" and "domain doesn't match" — collapsed to the same 404 at the API
layer).

## 3. New API

`apps/workspaces/api.py`:
- `GET /api/workspaces/joinable` → `list[JoinableWorkspaceOut]` (slug, display
  name, matched domain).
- `POST /api/workspaces/{slug}/join` → `WorkspaceOut`. Re-checks the domain
  server-side every call; nonexistent slug and non-matching domain both raise
  the service's `JoinError`, mapped to the same `HttpError(404, ...)` — no
  403 anywhere on this path, so it can't be used to enumerate tenants. Grants
  `EDITOR` via `ensure_member` (create-only), so a repeat call — or a call by an
  existing `viewer` — never changes an existing role.

New schema `JoinableWorkspaceOut` in `apps/workspaces/schemas.py`; `WorkspaceOut`
and `WorkspaceCreateIn`'s comments updated for the renamed field.

## 4. Frontend

- `frontend/src/api/workspaces.ts`: added `listJoinableWorkspaces()` and
  `joinWorkspace(slug)`.
- `frontend/src/pages/FirstRunPage.tsx`: fetches the joinable list whenever the
  caller has zero workspaces (skipped once `state === 'ready'`), and renders a
  "Join a workspace" section with a Join button per row and the matched-domain
  reason — additive to the existing create-form / needs-invite paths, never
  replacing them. `firstRun.ts`'s state machine itself is untouched (its pinned
  tests still pass unmodified).
- `frontend/src/api/generated.ts` regenerated from the live OpenAPI schema
  (dumped via the same recipe `regen-openapi.yml` uses) and committed; confirmed
  it contains `JoinableWorkspaceOut` and both new operations.
- Fixed two pre-existing test files that constructed a `WorkspaceOut`-shaped
  mock/fixture using the old field name (`InviteAcceptPage.test.tsx`) or an
  incomplete `@/api/workspaces` mock that didn't yet export
  `listJoinableWorkspaces` (`router.test.tsx` — FirstRunPage's new effect threw
  an unhandled rejection in the tests that render it, even though assertions
  still passed).

`npm run build` (tsc -b + vite build) is clean. `npm run test -- --run`: 915/915
pass, zero unhandled errors (down from 2 before the `router.test.tsx` mock fix).

## 5. Call sites removed (31, across 19 files)

- `apps/agents/api.py` — `_visible_agent_workspace_ids`, `upsert_agent` (2)
- `apps/agent_runs/api.py` — `_agent_or_404` (1)
- `apps/projects/api.py` — `_scoped_project_queryset`, `_member_project` (2)
- `apps/storyboards/api.py` — storyboard detail auth branch (1)
- `apps/harness/api.py` — `_agent_or_404`, `_runner_read_q`,
  `_runner_visibility_q`, `pair_runner`, `_turn_or_404`'s session-turn branch,
  `grant_runner_admin`, `_project_workspace_or_404`, `enqueue_turn`'s project
  branch, `list_turns` (9)
- `apps/harness/api_schedules.py` — `_visible_workspace_ids` (1)
- `apps/harness/services.py` — `list_visible_sessions` (1)
- `apps/harness/schedule_services.py` — `_resolve_agent` (1)
- `apps/canopy_sessions/api.py` — `_visible_slugs` (1)
- `apps/api/tenancy.py` — `WorkspaceResolveMiddleware.__call__` (1)
- `apps/timeline/api.py` — the events-list handler (1)
- `apps/runs/api.py` — `_workspace_slugs`, `get_run_release` (2)
- `apps/tokens/embed_api.py` — `list_embeddable_agents` (1)
- `apps/tokens/exchange_api.py` — the token-exchange handler (1)
- `apps/shareouts/api.py` — `list_shareouts` (1)
- `apps/reviews/api.py` — `list_reviews` (1)
- `apps/walkthroughs/api.py` — the walkthroughs-list handler (1)
- `apps/realtime/snapshot.py` — `supervisor_snapshot` (1)
- `apps/workspaces/services.py` — internal calls inside `request_workspace_slugs`
  and `workspace_slugs_for_user_id` (2), plus the function definition itself

Every removal was a one-line deletion of `wsvc.auto_join_workspaces(...)` (or
the equivalent bare call inside `services.py`), except `apps/runs/api.py`'s
`get_run_release`, where the call was the sole statement inside an
`if request.user.is_authenticated:` block — that whole now-empty block was
removed too (the endpoint's real access control lives in
`aggregate.build_release`'s `readable_by`/`_is_member` check, which is
independent of auto-join and untouched).

## 6. Tests rewritten (not deleted) — behavior assertions that no longer hold

- `apps/workspaces/tests/test_services.py::test_auto_join_adds_matching_domain_user_only`
  → split into `test_joinable_workspaces_lists_matching_domain_only` and
  `test_join_workspace_grants_editor_and_is_domain_gated`, exercising the new
  `joinable_workspaces`/`join_workspace` functions instead of the deleted one.
- `apps/agents/tests/test_workspace_scoping.py::test_domain_teammate_auto_joins_and_sees_agent`
  → `test_domain_teammate_no_longer_auto_joins_gets_404_and_empty_list`: asserts
  404/empty-list instead of 200/listed, plus that no membership row was created
  as a side effect.
- `apps/projects/tests/test_workspace_scoping.py::test_domain_teammate_auto_joins_and_sees_project`
  → same rewrite, project surface.
- `apps/shareouts/tests/test_workspace_scoping.py::test_domain_teammate_auto_joins_and_sees_shareout`
  → same rewrite, shareouts surface.
- `apps/reviews/tests/test_workspace_scoping.py::test_domain_teammate_auto_joins_and_sees_review`
  → same rewrite, reviews surface.
- `apps/walkthroughs/tests/test_workspace_scoping.py::test_domain_teammate_auto_joins_and_sees_walkthrough`
  → same rewrite, walkthroughs surface.
- `tests/test_harness_emdash_sessions.py::test_list_auto_joins_a_domain_matching_user_with_no_membership_row`
  → `test_list_no_longer_auto_joins_a_domain_matching_user_with_no_membership_row`:
  asserts an empty session list and no membership row instead of the old
  auto-joined list.
- `tests/test_token_exchange.py`:
  - `test_exchange_organic_membership_has_no_provisioning_provenance` — its
    "organic" membership used to come from auto-join with no explicit grant;
    rewritten to create that membership directly (`ensure_member`), since
    nothing in the exchange path grants membership on its own anymore.
  - `test_exchange_provisioning_wins_over_auto_join_for_the_granted_workspace`
    → `test_exchange_provision_role_holds_even_on_a_self_join_eligible_workspace`:
    same assertion (an explicit `provision_role` grant holds), reframed as "a
    self-join-eligible workspace doesn't change what provisioning does" since
    there's no more auto-join to race against.
  - `test_exchange_auto_join_still_applies_to_other_domain_workspaces` →
    `test_exchange_grants_nothing_in_other_domain_workspaces`: the OLD
    assertion (a second domain-matching workspace gets auto-joined too) is now
    false; rewritten to assert that workspace gets **no** membership at all.

New required tests added (per brief §5), all in `apps/workspaces/tests/test_api.py`:
`test_joinable_lists_matching_workspace_and_omits_others`,
`test_join_creates_editor_membership_and_is_idempotent`,
`test_join_cannot_be_used_to_elevate_an_existing_viewer` (pins the
create-only/no-elevation invariant), `test_join_nonmatching_domain_gets_404_not_403`,
`test_join_nonexistent_slug_gets_the_same_404`. Plus service-level coverage in
`apps/workspaces/tests/test_services.py` (see above) and the "auto-join is
really gone" proofs listed above (agents/projects/shareouts/reviews/walkthroughs/
harness-sessions).

### Tests that only needed the field renamed (no behavior change)

Straightforward `auto_join_domains` → `self_join_domains` renames, with no
change to what's being asserted: `apps/workspaces/tests/test_models.py`,
`test_api.py` (the pre-existing tests), `test_audit_auto_join.py`,
`apps/mcp/tests/test_schedule_tools.py`, `apps/issues/tests/test_api.py`,
`apps/runs/tests/factories.py` (`make_workspace`'s `auto_join_domains` kwarg →
`self_join_domains`), and the `auto_join_domains=[]`/`auto_join=()` fixture
plumbing in `tests/test_item_dispatch.py`, `test_schedule_services_crud.py`,
`test_harness_authz.py`, `test_agent_runs_authz.py`, `test_schedule_week.py`,
`test_claim_schedule_parity.py`, `test_schedule_authz.py`,
`test_agent_out_workspace.py`, `test_agents_turns_api.py`, `test_schedule_api.py`,
plus `apps/workspaces/testing.py` (`a_workspace`'s docstring) and
`apps/workspaces/management/commands/audit_auto_join.py` (field references +
wording; command name left unchanged since the brief didn't ask for a rename
there and an ops runbook may reference it by name).

### Two tests broken by the rename itself, not by removing auto-join

Both `apps/agents/tests/test_workspace_not_null_migration.py::test_falls_back_to_the_default_workspace_when_there_is_no_evidence`
and `apps/agents/tests/test_workspace_backfill.py::test_backfill_scopes_existing_agent_and_members`
call an **immutable** data-migration function (`agents/0013`'s `_resolve_target`,
`agents/0007`'s `backfill`) directly against `django.apps.apps` (the live,
fully-migrated model registry) as a stand-in for the historical one Django
actually passes a `RunPython` operation. Both migrations still literally write
`Workspace.objects.create(...auto_join_domains=...)` / `get_or_create(...,
defaults={"auto_join_domains": ...})` — correctly, since they're immutable and
that was the real column name when they were written. I verified empirically
(via `MigrationLoader`) that both migrations sort **before**
`workspaces/0008` in Django's actual forward plan, so this is safe in every
real deployment. It only broke in these two tests because they substitute the
already-fully-renamed live registry. Fixed with a tiny per-test proxy (`_CompatApps`
wrapping just the `Workspace.objects` manager to translate the one stale kwarg)
so the real, unmodified migration functions are still exercised end to end
rather than papered over.

## 7. Confirmation: no existing membership was revoked

`auto_join_workspaces` only ever called `ensure_member`, which is
`get_or_create`-shaped and only INSERTs — it contains no `DELETE`, `.update()`,
or role-lowering logic anywhere in its body (verified by reading it before
deletion). Every call site removed was a bare invocation with no other side
effect. Therefore removing 31 call sites plus the function itself deletes only
a source of *future* membership creation, never touches an existing
`WorkspaceMembership` row, and cannot revoke anything. Everyone auto-join
previously enrolled stays enrolled; only a *new*, never-yet-touched
domain-matching user now needs to click Join.

## Verification

- `uv run pytest -q` → **2551 passed, 1 skipped**.
- `cd frontend && npm run build` → clean (`tsc -b && vite build`).
- `cd frontend && npm run test -- --run` → **915 passed**, no unhandled errors.
- `python manage.py makemigrations --check --dry-run` → "No changes detected".
