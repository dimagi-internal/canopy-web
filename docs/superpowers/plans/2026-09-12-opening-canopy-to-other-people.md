# Opening Canopy To Other People — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make canopy-web usable by a power user who is not Jonathan — close the four
first-run dead ends, add a route-derived in-app guide that cannot silently miss a
surface, and publish a public explainer with live counts.

**Architecture:** Three independent parts. **A1** wires four existing API writes to UI
that does not exist (workspace create, PAT management, a real first-run screen).
**B** adds a colocated descriptor registry keyed by the route table, a `/guide` page that
renders it grouped by the existing nav, and one fast two-direction coverage test. **C**
adds an anonymous aggregates-only `/api/system/public-stats` and a chrome-less public
explainer page on `PublicLayout`. B and C share one registry of user paths so the public
promise cannot drift from the internal docs.

**Tech Stack:** Django 5 + Django Ninja 1.x + Pydantic v2 (backend), React 19 + Vite +
Tailwind 4 (frontend), pytest (backend tests), vitest (frontend tests), `openapi-typescript`
for generated API types.

**Spec:** `docs/superpowers/specs/2026-09-12-opening-canopy-to-other-people-design.md`

## Global Constraints

- **Design tokens only.** Never introduce raw Tailwind palette literals (`stone-*`,
  `orange-*`, `zinc-*`, `slate-*`, `red-*`, `amber-*`, `emerald-*`, `sky-*`, `violet-*`).
  Use `bg-background`, `bg-card`, `bg-muted`, `border-border`, `text-foreground`,
  `text-foreground-secondary`, `text-muted-foreground`, `text-foreground-subtle`,
  `bg-primary`/`text-primary`, and the status tokens `success`/`warning`/`info`/`special`/`destructive`.
- **All schemas subclass `StrictModel`** from `apps.common.schemas`.
- **Errors are RFC 7807.** Raise `ProblemError` from `apps.api.errors`, or `HttpError`
  where the surrounding file already does.
- **When you change an `apps/**/schemas.py` or `api.py`, regenerate frontend types and
  commit them**: `cd frontend && npm run gen:api` with the backend up on :8000, or
  `npm run gen:api:local` against a dumped `openapi.json`.
- **A public endpoint needs TWO changes, not one**: `auth=None` on the route AND an entry
  in `PUBLIC_PATH_PREFIXES` (or a matcher) in `apps/common/middleware.py`. The login
  middleware is default-deny; `auth=None` alone still bounces an anonymous caller to Google.
- **No new CI workflow and no new CI job.** Everything runs inside the existing
  `uv run pytest` and `cd frontend && npm run test` commands.
- **Backend tests** go in root `tests/test_*.py` for cross-cutting concerns, or
  `apps/<app>/tests/test_*.py` where that app already has a `tests/` package
  (`apps/workspaces/tests/` does).
- **Frontend tests** are vitest, colocated as `<module>.test.ts(x)`. **jsdom 29 and
  `@testing-library/react` 16 ARE installed and used** — there are ~10 component tests
  calling `render()` (`PublicHeader.test.tsx`, `ChatSessionsPanel.test.tsx`,
  `RunnerAssignments.test.tsx`, …), so mounting a component in a test is available to you.
  (An earlier revision of this plan claimed otherwise, propagating a stale comment at
  `frontend/src/workspace/resolveActiveWorkspace.ts:2`. That comment is wrong; the plan
  was wrong for repeating it.) Still prefer extracting decision logic into a plain `.ts`
  module and testing it directly — it is faster and less brittle than asserting on
  rendered output — but a component test is a legitimate choice where the behaviour only
  exists once mounted.
- **A task that changes a Pydantic response schema MUST run `cd frontend && npm run build`,
  not just the backend tests.** Fields in `generated.ts` are non-optional, so adding one
  to a response model breaks every test file that constructs a mock of it — and those
  files are usually nowhere near your diff. Task 1 shipped exactly this break (two
  `tsc` failures in `PublicHeader.test.tsx` and `NoteComposer.test.tsx`) because its
  verification ran `pytest` only. Task 10 adds `PublicStatsOut`; do not repeat it.
- **Open PRs with auto-merge armed**: `gh pr merge <n> --auto`. Never pass `--squash` or
  any strategy flag — the merge queue owns the strategy and a strategy flag leaves
  auto-merge UNARMED. Verify with `gh pr view <n> --json autoMergeRequest`.

---

## File Structure

**Part A1 — first run**

| File | Responsibility |
|---|---|
| `apps/common/schemas.py` (modify) | `MeOut` gains `can_create_workspace: bool` |
| `apps/common/api.py` (modify, ~line 42) | `me()` populates it from `workspaces.services.can_create_workspace` |
| `frontend/src/api/workspaces.ts` (modify) | `createWorkspace()` client call |
| `frontend/src/api/tokens.ts` (create) | List / mint / revoke PAT client calls |
| `frontend/src/pages/FirstRunPage.tsx` (create) | The zero-workspace screen |
| `frontend/src/pages/firstRun.ts` (create) | Pure state logic — which of the three first-run states applies |
| `frontend/src/pages/firstRun.test.ts` (create) | Tests for that logic |
| `frontend/src/router.tsx` (modify) | `RootRedirect`/`TenantRedirect` render `FirstRunPage` instead of `null` |
| `frontend/src/components/AppLayout/AppLayout.tsx` (modify, ~line 188) | Split the switcher guard from the create affordance |
| `frontend/src/components/settings/TokensPanel.tsx` (create) | PAT list / mint / revoke UI |
| `frontend/src/pages/SettingsPage.tsx` (modify) | Mount `TokensPanel` |

**Part B — the self-documenting app**

| File | Responsibility |
|---|---|
| `frontend/src/router.tsx` (modify) | Extract the inline route array to `export const routeTable` |
| `frontend/src/guide/surfaces.ts` (create) | One descriptor per documented route — the registry |
| `frontend/src/guide/paths.ts` (create) | The five ways in; shared by `/guide` and the public page |
| `frontend/src/guide/coverage.ts` (create) | Pure helpers: flatten the route table, classify redirects |
| `frontend/src/guide/coverage.test.ts` (create) | The two-direction coverage test |
| `frontend/src/pages/GuidePage.tsx` (create) | Renders descriptors grouped by `NAV_GROUPS` |

**Part C — the public explainer**

| File | Responsibility |
|---|---|
| `apps/system/stats.py` (create) | Pure aggregate computation, no Django request objects |
| `apps/system/schemas.py` (modify) | `PublicStatsOut` |
| `apps/system/api.py` (modify) | `GET /public-stats`, `auth=None` |
| `apps/common/middleware.py` (modify, `PUBLIC_PATH_PREFIXES`) | Allowlist `/api/system/public-stats` |
| `tests/test_public_stats.py` (create) | Anonymous access + the no-leak assertion |
| `frontend/src/api/publicStats.ts` (create) | Anonymous client call |
| `frontend/src/pages/AboutPage.tsx` (create) | The public explainer |
| `frontend/src/guide/components.ts` (create) | The five-component view data, shared by both pages |

---

## Task 1: Expose workspace-creation eligibility on `/api/me/`

The first-run screen has **three** states, not two, and the third is a security
invariant. `apps/workspaces/services.py:497` gates creation: an *invite-admitted* user
who holds no membership must NOT be able to create a workspace, because that would make
invite-admission transitively delegable (create a workspace → mint invites → each
invitee clears the login gate too). This is the F1 security finding. So a first-run
screen that always shows a create form would 403 for exactly those users. The screen has
to know, which means `/api/me/` has to say.

**Files:**
- Modify: `apps/common/schemas.py:38-43` (`MeOut`)
- Modify: `apps/common/api.py:42-57` (`me`)
- Test: `tests/test_me_can_create_workspace.py`

**Interfaces:**
- Consumes: `apps.workspaces.services.can_create_workspace(user) -> bool` (exists).
- Produces: `MeOut.can_create_workspace: bool`, reaching the frontend as
  `components["schemas"]["MeOut"]["can_create_workspace"]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_me_can_create_workspace.py`:

```python
"""GET /api/me/ reports whether the caller may create a workspace.

The first-run screen needs this to avoid offering a button that 403s. The gate
itself is apps.workspaces.services.can_create_workspace (the F1 finding).
"""
import pytest
from django.contrib.auth import get_user_model

from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()


@pytest.fixture
def allowlisted(db):
    return User.objects.create_user(username="ally", email="ally@dimagi.com")


@pytest.fixture
def invited(db):
    return User.objects.create_user(username="guest", email="guest@example.org")


def test_allowlisted_domain_may_create(client, allowlisted):
    client.force_login(allowlisted)
    body = client.get("/api/me/").json()
    assert body["can_create_workspace"] is True


def test_invite_admitted_with_no_membership_may_not_create(client, invited):
    client.force_login(invited)
    body = client.get("/api/me/").json()
    assert body["can_create_workspace"] is False


def test_invite_admitted_with_a_membership_may_create(client, invited):
    ws = Workspace.objects.create(slug="acme", display_name="Acme")
    WorkspaceMembership.objects.create(
        workspace=ws, user=invited, role=WorkspaceMembership.OWNER
    )
    client.force_login(invited)
    body = client.get("/api/me/").json()
    assert body["can_create_workspace"] is True
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_me_can_create_workspace.py -v`
Expected: FAIL — `KeyError: 'can_create_workspace'` on all three tests.

- [ ] **Step 3: Add the field to `MeOut`**

In `apps/common/schemas.py`, extend `MeOut`:

```python
class MeOut(StrictModel):
    """Response for /api/me/."""

    email: EmailStr
    name: str
    avatar_url: str
    can_create_workspace: bool = False
    """Whether this caller may POST /api/workspaces/. False for an
    invite-admitted user holding no membership — see the F1 finding in
    apps.workspaces.services.can_create_workspace. The first-run screen reads
    this so it never offers a button that 403s."""
```

- [ ] **Step 4: Populate it in the view**

In `apps/common/api.py`, inside `me()`, add the import at the top of the function body
(a local import — `apps.common` is framework and must not grow a module-level dependency
on another app's services at import time):

```python
def me(request: HttpRequest) -> MeOut:
    from apps.workspaces.services import can_create_workspace

    user = request.user
    avatar_url = ""
    social = (
        user.socialaccount_set.filter(provider="google").first()
        if hasattr(user, "socialaccount_set")
        else None
    )
    if social:
        avatar_url = social.extra_data.get("picture", "") or ""
    return MeOut(
        email=user.email,
        name=(user.get_full_name() or user.username or user.email),
        avatar_url=avatar_url,
        can_create_workspace=can_create_workspace(user),
    )
```

- [ ] **Step 5: Run the test and confirm it passes**

Run: `uv run pytest tests/test_me_can_create_workspace.py -v`
Expected: 3 passed.

- [ ] **Step 6: Regenerate the frontend types**

With the backend running on :8000:

```bash
cd frontend && npm run gen:api
```

Confirm `can_create_workspace` now appears in `src/api/generated.ts` under `MeOut`:

```bash
grep -n -A6 "MeOut:" src/api/generated.ts | head -12
```

- [ ] **Step 7: Run the full backend suite to check nothing else asserted MeOut's shape**

Run: `uv run pytest -q`
Expected: no new failures. `StrictModel` forbids unknown fields on *input*; adding an
output field is safe, but a test asserting an exact response dict would break — fix any
such test by adding the new key.

- [ ] **Step 8: Commit**

```bash
git add apps/common/schemas.py apps/common/api.py tests/test_me_can_create_workspace.py frontend/src/api/generated.ts
git commit -m "api: /api/me/ reports workspace-creation eligibility

The first-run screen must not offer a create button to an invite-admitted
user with no membership — can_create_workspace 403s for exactly them (the
F1 finding). Surfacing the gate is what lets the UI show the right thing."
```

---

## Task 2: The first-run screen replaces the blank page

`frontend/src/router.tsx:126` and `:135` both `return null` when the user has no
workspace. A signed-in user with no membership therefore sees an empty app shell — no
error, no message, nothing. This is the literal first screen of the power-user rollout.

The state logic goes in a plain `.ts` module because a pure function is the cheapest,
least brittle place to pin a three-way decision — not because component testing is
unavailable (it is available; see Global Constraints).

**No backend test needed for the create endpoint.** `apps/workspaces/tests/test_api.py`
already covers both error paths this screen must handle: the 403 for an ineligible caller
(line 68) and the 409 for a duplicate slug (line 105). What is untested is the *screen's*
three-state logic, which is what `firstRun.test.ts` below covers.

**Files:**
- Create: `frontend/src/pages/firstRun.ts`
- Create: `frontend/src/pages/firstRun.test.ts`
- Create: `frontend/src/pages/FirstRunPage.tsx`
- Modify: `frontend/src/api/workspaces.ts`
- Modify: `frontend/src/router.tsx:120-140`

**Interfaces:**
- Consumes: `MeOut.can_create_workspace` (Task 1); `useWorkspace()` from
  `@/workspace/WorkspaceProvider` returning `{ workspaces, active, loading, refresh }`
  — verified: `refresh: () => Promise<void>` re-fetches the membership list and its own
  doc comment states a page mutating the caller's workspace state must call it;
  `getMe()` from `@/api/me`.
- Produces: `firstRunState(args) -> 'loading' | 'can-create' | 'needs-invite' | 'ready'`,
  and `createWorkspace(slug, displayName) -> Promise<WorkspaceOut>` in `api/workspaces.ts`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/pages/firstRun.test.ts`:

```typescript
import { describe, it, expect } from 'vitest'
import { firstRunState } from './firstRun'

describe('firstRunState', () => {
  it('is loading while either the workspace list or me is unresolved', () => {
    expect(firstRunState({ loading: true, workspaceCount: 0, canCreate: null })).toBe('loading')
    expect(firstRunState({ loading: false, workspaceCount: 0, canCreate: null })).toBe('loading')
  })

  it('is ready once the user has at least one workspace', () => {
    expect(firstRunState({ loading: false, workspaceCount: 1, canCreate: false })).toBe('ready')
  })

  it('offers creation to an eligible user with no workspace', () => {
    expect(firstRunState({ loading: false, workspaceCount: 0, canCreate: true })).toBe('can-create')
  })

  it('asks an ineligible user for an invite instead of offering a button that 403s', () => {
    expect(firstRunState({ loading: false, workspaceCount: 0, canCreate: false })).toBe('needs-invite')
  })

  it('prefers ready over creation when the user already belongs somewhere', () => {
    // Guards the ordering: an eligible user WITH a workspace must be routed on,
    // not parked on the first-run screen.
    expect(firstRunState({ loading: false, workspaceCount: 2, canCreate: true })).toBe('ready')
  })
})
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `cd frontend && npx vitest run src/pages/firstRun.test.ts`
Expected: FAIL — cannot resolve `./firstRun`.

- [ ] **Step 3: Write the logic module**

Create `frontend/src/pages/firstRun.ts`:

```typescript
/**
 * Which first-run state applies. Extracted as a pure function so the three-way
 * decision can be pinned directly, without mounting anything — cheaper and less
 * brittle than asserting on rendered output.
 *
 * There are THREE zero-workspace states, not one. An invite-admitted user who
 * holds no membership may not create a workspace (the F1 finding — see
 * apps/workspaces/services.py::can_create_workspace), so offering them a button
 * would 403. `canCreate: null` means /api/me/ has not answered yet.
 */
export interface FirstRunInput {
  loading: boolean
  workspaceCount: number
  canCreate: boolean | null
}

export type FirstRunState = 'loading' | 'can-create' | 'needs-invite' | 'ready'

export function firstRunState({ loading, workspaceCount, canCreate }: FirstRunInput): FirstRunState {
  if (workspaceCount > 0) return 'ready'
  if (loading || canCreate === null) return 'loading'
  return canCreate ? 'can-create' : 'needs-invite'
}
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `cd frontend && npx vitest run src/pages/firstRun.test.ts`
Expected: 5 passed.

- [ ] **Step 5: Add the `createWorkspace` client call**

Append to `frontend/src/api/workspaces.ts`, matching the `apiV2` style already in that
file:

```typescript
export async function createWorkspace(
  slug: string,
  displayName: string,
): Promise<{ slug: string } | { error: string }> {
  const res = await apiV2.POST('/api/workspaces/', {
    body: { slug, display_name: displayName },
  })
  // Branch on `res.response.ok`, NOT on `res.error` — this endpoint declares only a
  // 201 in the OpenAPI schema, so `res.error` narrows to `never` and `if (res.error)`
  // fails tsc. `frontend/src/api/workspaces.ts:32-36` documents this convention and
  // every other function in the file follows it.
  if (!res.response.ok) {
    // 409 = slug taken, 403 = not eligible (F1), 422 = bad slug charset.
    // The server's problem+json `detail` is the only message worth showing:
    // the slug rules are enforced by Workspace.SLUG_PATTERN server-side and
    // restating them here would be a second copy free to drift.
    const detail = (res.error as { detail?: string })?.detail
    return { error: detail || 'Could not create the workspace.' }
  }
  return { slug: res.data.slug }
}
```

- [ ] **Step 6: Write the first-run screen**

Create `frontend/src/pages/FirstRunPage.tsx`:

```tsx
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { getMe } from '@/api/me'
import { createWorkspace } from '@/api/workspaces'
import { firstRunState } from './firstRun'

/**
 * What a brand-new user sees. Replaces the two `return null` sites in
 * router.tsx, which rendered a blank page for anyone with no workspace
 * membership — the first screen of the power-user rollout.
 *
 * Doubles as documentation: this is the only page every new user is
 * guaranteed to read, so it explains what a workspace IS rather than just
 * asking for a slug.
 */
export function FirstRunPage() {
  const { workspaces, loading, refresh } = useWorkspace()
  const navigate = useNavigate()
  const [canCreate, setCanCreate] = useState<boolean | null>(null)
  const [slug, setSlug] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    void getMe().then((me) => setCanCreate(me?.can_create_workspace ?? false))
  }, [])

  const state = firstRunState({
    loading,
    workspaceCount: workspaces.length,
    canCreate,
  })

  if (state === 'loading' || state === 'ready') return null

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    const res = await createWorkspace(slug.trim(), displayName.trim() || slug.trim())
    setBusy(false)
    if ('error' in res) {
      setError(res.error)
      return
    }
    // WorkspaceProvider fetches the membership list once on mount and never
    // invalidates it, so a brand-new membership is invisible until something
    // re-fetches. `refresh()` is the provider's documented mechanism for exactly
    // this ("a page that mutates the CALLER's own role in a workspace must call
    // this afterward") — use it rather than a full page reload.
    await refresh()
    navigate(`/w/${res.slug}`)
  }

  return (
    <div className="mx-auto max-w-xl px-6 py-16">
      <h1 className="text-lg font-semibold text-foreground">Welcome to Canopy</h1>
      <p className="mt-3 text-[13px] leading-relaxed text-foreground-secondary">
        Canopy runs a fleet of AI agents and keeps the record of what they do. Everything
        that belongs to a team — projects, agents, chats, demos — lives inside a{' '}
        <span className="font-medium text-foreground">workspace</span>. You are not in one yet.
      </p>

      {state === 'can-create' ? (
        <form onSubmit={submit} className="mt-8 space-y-4">
          <div>
            <label htmlFor="ws-slug" className="block text-xs font-medium text-foreground-secondary">
              Workspace address
            </label>
            <input
              id="ws-slug"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="acme"
              required
              className="mt-1 w-full rounded border border-input bg-input px-2 py-1.5 text-[13px] text-foreground"
            />
            <p className="mt-1 text-[11px] text-muted-foreground">
              Used in every URL: /w/&lt;address&gt;. Lowercase letters, numbers and dashes.
            </p>
          </div>
          <div>
            <label htmlFor="ws-name" className="block text-xs font-medium text-foreground-secondary">
              Display name <span className="text-muted-foreground">(optional)</span>
            </label>
            <input
              id="ws-name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="Acme"
              className="mt-1 w-full rounded border border-input bg-input px-2 py-1.5 text-[13px] text-foreground"
            />
          </div>
          {error ? <p className="text-[13px] text-destructive">{error}</p> : null}
          <button
            type="submit"
            disabled={busy || !slug.trim()}
            className="rounded bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {busy ? 'Creating…' : 'Create workspace'}
          </button>
          <p className="text-[12px] text-muted-foreground">
            Already invited to one? Open the /invite/… link a colleague sent you instead.
          </p>
        </form>
      ) : (
        <div className="mt-8 rounded-lg border border-border bg-card p-4">
          <h2 className="text-sm font-semibold text-foreground">You need an invite</h2>
          <p className="mt-2 text-[13px] leading-relaxed text-foreground-secondary">
            Your account can join a workspace but cannot create one. Ask a workspace owner
            to invite you — they can do it from their workspace&apos;s Members page — and open
            the /invite/… link they send you.
          </p>
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 7: Wire it into the router**

In `frontend/src/router.tsx`, import it and replace both `return null` sites:

```tsx
import { FirstRunPage } from './pages/FirstRunPage'
```

In `TenantRedirect` (was line 126):

```tsx
  if (loading) return null
  if (!active) return <FirstRunPage />  // no membership yet — explain, don't blank
```

In `RootRedirect` (was line 135):

```tsx
function RootRedirect() {
  const { active, loading } = useWorkspace()
  if (loading) return null
  if (!active) return <FirstRunPage />
  return <Navigate to={`/w/${active}`} replace />
}
```

- [ ] **Step 8: Verify the build and the full frontend suite**

Run: `cd frontend && npm run build && npm run test`
Expected: build succeeds (tsc clean), all tests pass.

- [ ] **Step 9: Verify in the real app, not with curl**

canopy-web is a PWA and a stale service worker will serve an old bundle, so curl proves
nothing. Start the app (`uv run honcho start -f Procfile.dev`), sign in, and confirm:
a user with a workspace still lands on their workbench; then check the zero-workspace
case by visiting as a user with no membership (create one in the Django shell:
`User.objects.create_user(username="t", email="t@dimagi.com")`, then
`client.force_login`, or remove your own membership in a scratch database). You should
see the welcome copy and a create form, not a blank page.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/pages/firstRun.ts frontend/src/pages/firstRun.test.ts frontend/src/pages/FirstRunPage.tsx frontend/src/api/workspaces.ts frontend/src/router.tsx
git commit -m "ui: a new user gets a first-run screen, not a blank page

router.tsx returned null for anyone with no workspace membership, so a
signed-in new user saw an empty shell. Three states, not two: an
invite-admitted user with no membership cannot create a workspace (F1), so
the screen reads /api/me/ and asks them for an invite instead of offering a
button that 403s."
```

---

## Task 3: Always offer "create workspace" from the header

`frontend/src/components/AppLayout/AppLayout.tsx:188` reads
`if (workspaces.length <= 1) return null`, which conflates *nothing to switch between*
with *nothing you can do*. A user with one workspace has no way to make a second.

**Files:**
- Modify: `frontend/src/components/AppLayout/AppLayout.tsx:185-200`

**Interfaces:**
- Consumes: `useWorkspace()`, `getMe()`, `FirstRunPage`'s sibling `createWorkspace`
  (already added in Task 2).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Split the guard**

In `WorkspaceSwitcher`, keep hiding the `<select>` when there is nothing to switch
between, but render a create entry regardless. Replace the early return:

```tsx
function WorkspaceSwitcher() {
  const { workspaces, active } = useWorkspace()
  const navigate = useNavigate()
  // Hide the SWITCHER when there is nothing to switch between — but never hide
  // the way to make another one. Conflating those two is what left a
  // one-workspace user with no path to a second (and a zero-workspace user
  // with no path at all).
  if (workspaces.length <= 1) {
    return <NewWorkspaceLink />
  }
  return (
    <select
      /* ...existing props unchanged... */
    >
      {/* ...existing options unchanged... */}
    </select>
  )
}
```

- [ ] **Step 2: Add the link component**

In the same file, beside `WorkspaceSwitcher`:

```tsx
/** The one affordance that must never be conditional on how many workspaces
 *  you already have. Routes to the first-run screen, which owns the form. */
function NewWorkspaceLink() {
  return (
    <Link
      to="/new-workspace"
      className="text-xs text-muted-foreground hover:text-foreground"
    >
      + Workspace
    </Link>
  )
}
```

Use react-router `<Link>`, not a hand-built `href` off `BASE_URL` — `Link` applies the
router's basename itself, and this file already imports from `react-router-dom`
(`useNavigate`). Same reasoning as Task 9's links.

- [ ] **Step 3: Add the `/new-workspace` route**

In `frontend/src/router.tsx`, inside the `AppLayout` children, beside the other
personal/global routes:

```tsx
      { path: '/new-workspace', element: <FirstRunPage /> },
```

`FirstRunPage` already handles the `ready` state by rendering `null`, which is wrong for
this route — a user who already has a workspace must still see the form. Add an explicit
prop:

In `FirstRunPage.tsx`, change the signature and the guard:

```tsx
export function FirstRunPage({ alwaysOfferForm = false }: { alwaysOfferForm?: boolean }) {
```

After Task 2's fix round the guard is a SINGLE combined line —
`if (state === 'loading' || state === 'ready') return null` — and `canCreate` is a
plain `boolean` local derived from `useAuth()`. Replace that one line with:

```tsx
  if (state === 'loading') return null
  if (state === 'ready' && !alwaysOfferForm) return null
  // On /new-workspace an eligible user sees the form even though they already
  // belong somewhere; `needs-invite` still applies, because eligibility is the
  // server's call either way.
  const offerForm = alwaysOfferForm ? canCreate : state === 'can-create'
```

and use `offerForm` in place of `state === 'can-create'` in the JSX conditional.

Then pass the prop on that route only:

```tsx
      { path: '/new-workspace', element: <FirstRunPage alwaysOfferForm /> },
```

- [ ] **Step 4: Extend the logic test for the new route's behaviour**

Append to `frontend/src/pages/firstRun.test.ts`:

```typescript
  it('still reports ready for a member — /new-workspace opts out via a prop, not this fn', () => {
    // Documents the seam: firstRunState stays a pure description of the user's
    // standing. The route decides whether to show a form anyway.
    expect(firstRunState({ loading: false, workspaceCount: 3, canCreate: true })).toBe('ready')
  })
```

- [ ] **Step 5: Run the frontend suite and build**

Run: `cd frontend && npm run test && npm run build`
Expected: all pass, tsc clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/AppLayout/AppLayout.tsx frontend/src/router.tsx frontend/src/pages/FirstRunPage.tsx frontend/src/pages/firstRun.test.ts
git commit -m "ui: '+ Workspace' is always reachable from the header

The switcher hid itself at <=1 workspace, which also hid the only way to
create another. Hide the switcher, keep the create affordance."
```

---

## Task 4: Personal Access Token management on `/settings`

`POST /api/tokens/` and `DELETE /api/tokens/{pk}/` exist with zero UI
(`apps/tokens/api.py`). A PAT is the gate on two of the five ways in — pointing another
tool at the MCP surface, and setting up the plugin — and today the only way to get one is
to run a skill that requires the plugin you are trying to set up.

The raw value is returned **exactly once**, by `PersonalTokenCreatedOut`. The UI must
show it once and never pretend it can be retrieved again.

**This task is frontend-only — do not add backend tests.** `apps/tokens/tests/test_api.py`
already covers the API with 12 tests, including `test_revoke_other_users_token_404`,
`test_revoke_idempotent`, `test_list_tokens_only_mine` and
`test_create_token_defaults_to_expiring`. The spec's "404 on revoking someone else's
token" requirement is already satisfied there; duplicating it here would be a second copy
free to drift.

**Files:**
- Create: `frontend/src/api/tokens.ts`
- Create: `frontend/src/components/settings/TokensPanel.tsx`
- Modify: `frontend/src/pages/SettingsPage.tsx`

**Interfaces:**
- Consumes: `apiV2` from `@/api/client.v2`; the generated `PersonalTokenOut` /
  `PersonalTokenCreatedOut` schemas; the existing `CopyBlock` component in
  `SettingsPage.tsx` (used by `DebugAccessPanel`).
- Produces: `listTokens()`, `mintToken(label, ttlDays)`, `revokeToken(id)` in
  `api/tokens.ts`.

- [ ] **Step 1: Write the client module**

Create `frontend/src/api/tokens.ts`:

```typescript
/**
 * Personal Access Tokens. The raw value comes back from mint() exactly once —
 * PersonalTokenCreatedOut is the only shape that carries it, and the server
 * stores a hash. The UI must therefore show it immediately and never imply it
 * can be recovered.
 */
import { apiV2 } from './client.v2'
import { problemMessage } from './problem'
import type { components } from './generated'

export type PersonalToken = components['schemas']['PersonalTokenOut']
export type MintedToken = components['schemas']['PersonalTokenCreatedOut']

// Every function here branches on `res.response.ok`, NEVER on `res.error`.
// All three token endpoints declare ONLY a success response in the OpenAPI
// schema (200 / 201 / 204), so `res.error` narrows to `never` and
// `if (res.error)` fails tsc. `frontend/src/api/workspaces.ts:32-36` documents
// this trap and every function in that file follows the same rule. `res.error`
// is still safe to READ for a message — just not to branch on.

export async function listTokens(): Promise<PersonalToken[]> {
  const res = await apiV2.GET('/api/tokens/')
  if (!res.response.ok || !res.data) return []
  return res.data
}

export async function mintToken(
  label: string,
  ttlDays: number | null,
): Promise<MintedToken | { error: string }> {
  const res = await apiV2.POST('/api/tokens/', {
    body: { label, ttl_days: ttlDays },
  })
  if (!res.response.ok || !res.data) {
    return { error: problemMessage(res.error, 'Could not mint a token.') }
  }
  return res.data
}

export async function revokeToken(id: number): Promise<boolean> {
  const res = await apiV2.DELETE('/api/tokens/{pk}/', {
    params: { path: { pk: id } },
  })
  return res.response.ok
}
```

`problemMessage(error, fallback)` lives at `frontend/src/api/problem.ts:21` and already
handles the RFC 7807 shape (prefers `detail`, falls back to `title`, then to your string) —
use it rather than reaching into `.detail` by hand.

- [ ] **Step 2: Confirm the generated types already carry these schemas**

Run:

```bash
cd frontend && grep -c "PersonalTokenCreatedOut" src/api/generated.ts
```

Expected: a non-zero count. The tokens router is already registered
(`apps/api/api.py:175`), so no backend change and no regen is needed for this task. If
the count is 0, run `npm run gen:api` with the backend up and commit the result.

- [ ] **Step 3: Write the panel**

Create `frontend/src/components/settings/TokensPanel.tsx`:

```tsx
import { useEffect, useState } from 'react'
import { listTokens, mintToken, revokeToken, type PersonalToken } from '@/api/tokens'

/**
 * List / mint / revoke Personal Access Tokens.
 *
 * A PAT is how a machine caller authenticates (Authorization: Bearer <raw>) —
 * the canopy plugin, the MCP surface at /api/mcp/, and any script. It had no UI
 * at all, so the only way to get one was a skill that needs the plugin you are
 * trying to set up.
 */
export function TokensPanel() {
  const [tokens, setTokens] = useState<PersonalToken[]>([])
  const [label, setLabel] = useState('')
  const [minted, setMinted] = useState<string>('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function refresh() {
    setTokens(await listTokens())
  }

  useEffect(() => {
    void refresh()
  }, [])

  async function onMint(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    const res = await mintToken(label.trim(), null)
    setBusy(false)
    if ('error' in res) {
      setError(res.error)
      return
    }
    setMinted(res.raw)
    setLabel('')
    await refresh()
  }

  async function onRevoke(t: PersonalToken) {
    if (!window.confirm(`Revoke "${t.label}"? Anything using it stops working immediately.`)) return
    if (await revokeToken(t.id)) await refresh()
    else setError('Could not revoke that token.')
  }

  return (
    <section className="rounded-lg border border-border bg-card p-4">
      <h2 className="text-sm font-semibold text-foreground">Personal access tokens</h2>
      <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">
        How a machine authenticates as you — the canopy plugin, the MCP endpoint, or your
        own scripts. Sent as <code>Authorization: Bearer &lt;token&gt;</code>.
      </p>

      {minted ? (
        <div className="mt-3 rounded border border-warning/30 bg-warning/10 p-3">
          <p className="text-[12px] font-medium text-warning">
            Copy this now — it is not shown again.
          </p>
          <div className="mt-2 flex items-center gap-2">
            <code className="flex-1 truncate rounded bg-muted px-2 py-1 text-[12px] text-foreground">
              {minted}
            </code>
            <button
              type="button"
              onClick={() => void navigator.clipboard.writeText(minted)}
              className="rounded bg-primary px-2 py-1 text-[12px] font-medium text-primary-foreground hover:bg-primary/90"
            >
              Copy
            </button>
            <button
              type="button"
              onClick={() => setMinted('')}
              className="text-[12px] text-muted-foreground hover:text-foreground"
            >
              Done
            </button>
          </div>
        </div>
      ) : null}

      <form onSubmit={onMint} className="mt-3 flex items-end gap-2">
        <div className="flex-1">
          <label htmlFor="pat-label" className="block text-xs font-medium text-foreground-secondary">
            What is it for?
          </label>
          <input
            id="pat-label"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="my laptop's canopy plugin"
            required
            className="mt-1 w-full rounded border border-input bg-input px-2 py-1.5 text-[13px] text-foreground"
          />
        </div>
        <button
          type="submit"
          disabled={busy || !label.trim()}
          className="rounded bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {busy ? 'Minting…' : 'Mint token'}
        </button>
      </form>
      {error ? <p className="mt-2 text-[13px] text-destructive">{error}</p> : null}

      {tokens.length === 0 ? (
        <p className="mt-4 text-[13px] text-muted-foreground">No tokens yet.</p>
      ) : (
        <table className="mt-4 w-full text-[12px]">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1 font-medium">Label</th>
              <th className="py-1 font-medium">Created</th>
              <th className="py-1 font-medium">Last used</th>
              <th className="py-1 font-medium">Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {tokens.map((t) => (
              <tr key={t.id} className="border-t border-border">
                <td className="py-1.5 text-foreground">{t.label}</td>
                <td className="py-1.5 text-muted-foreground">{t.created_at.slice(0, 10)}</td>
                <td className="py-1.5 text-muted-foreground">
                  {t.last_used_at ? t.last_used_at.slice(0, 10) : 'never'}
                </td>
                <td className="py-1.5 text-muted-foreground">
                  {t.revoked_at ? 'revoked' : t.expires_at ? `expires ${t.expires_at.slice(0, 10)}` : 'active'}
                </td>
                <td className="py-1.5 text-right">
                  {t.revoked_at ? null : (
                    <button
                      type="button"
                      onClick={() => void onRevoke(t)}
                      className="text-destructive hover:underline"
                    >
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
```

- [ ] **Step 4: Mount it on the settings page**

In `frontend/src/pages/SettingsPage.tsx`, import and render it after the Presence
section and before `DebugAccessPanel`:

```tsx
import { TokensPanel } from '@/components/settings/TokensPanel'
```

```tsx
        <TokensPanel />
```

- [ ] **Step 5: Build and run the frontend suite**

Run: `cd frontend && npm run build && npm run test`
Expected: tsc clean, tests pass.

- [ ] **Step 6: Verify against the running app**

With the app running and signed in, open `/settings`: mint a token, confirm the raw value
appears once with a warning, reload the page and confirm the raw value is gone but the row
remains, then revoke it and confirm the row reads `revoked`. Then check the token actually
works: `curl -H "Authorization: Bearer <raw>" http://localhost:8000/api/me/`.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api/tokens.ts frontend/src/components/settings/TokensPanel.tsx frontend/src/pages/SettingsPage.tsx
git commit -m "ui: manage personal access tokens from /settings

POST/DELETE /api/tokens/ existed with no UI, so the only way to get a PAT
was a skill requiring the plugin the PAT is for. Raw value is shown exactly
once, because that is all the server can ever return."
```

---

## Task 5: Extract `routeTable` from the router

Recovering paths from a *built* `createBrowserRouter` result is awkward; reading them
from an exported array is three lines. Pure refactor — no behaviour change.

**Files:**
- Modify: `frontend/src/router.tsx:178-297`

**Interfaces:**
- Produces: `export const routeTable: RouteObject[]` — the same array previously passed
  inline to `guarded()`. Consumed by Task 6's coverage test.

- [ ] **Step 1: Extract the array**

In `frontend/src/router.tsx`, change:

```tsx
export const router = createBrowserRouter(guarded([
  { /* ... */ },
]), { basename: /* ... */ })
```

to:

```tsx
/**
 * The route table, as data.
 *
 * Exported so the guide's coverage test can read it: every documented surface
 * must have a descriptor in guide/surfaces.ts, and every descriptor must name a
 * real route. Extracting paths from a BUILT router is awkward; reading them from
 * this array is three lines.
 */
export const routeTable: RouteObject[] = [
  { /* ...the existing array, verbatim and unchanged... */ },
]

export const router = createBrowserRouter(guarded(routeTable), {
  basename: import.meta.env.BASE_URL.replace(/\/$/, '') || '/',
})
```

Move nothing else. The array's contents, ordering and comments stay byte-identical —
ordering is load-bearing (the catch-all must stay last).

- [ ] **Step 2: Verify the build is clean**

Run: `cd frontend && npm run build`
Expected: tsc clean. `RouteObject` is already imported in this file (used by `guarded`).

- [ ] **Step 3: Verify the app still routes**

Run the app and click through three routes including a tenant one (`/w/<ws>/agents`), a
global one (`/settings`) and a redirect (`/timeline` → `/w/<ws>/timeline`). A reordering
mistake shows up as a route resolving to the catch-all.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/router.tsx
git commit -m "refactor: export routeTable as data

No behaviour change. The guide's coverage test needs to read the route
paths, and reading them from an exported array beats introspecting a built
router."
```

---

## Task 6: The descriptor registry and its coverage test

**Files:**
- Create: `frontend/src/guide/coverage.ts`
- Create: `frontend/src/guide/surfaces.ts`
- Create: `frontend/src/guide/coverage.test.ts`

**Interfaces:**
- Consumes: `routeTable` (Task 5).
- Produces:
  - `flattenRoutePaths(routes: RouteObject[]): string[]` — every declared path, with
    child paths joined onto their parent.
  - `isDocumentable(path: string): boolean` — false for redirects, legacy aliases and
    the catch-all.
  - `SURFACES: SurfaceDescriptor[]` where
    `SurfaceDescriptor = { path: string; title: string; audience: string; what: string; needsFirst?: string; actions?: string[] }`.
  - `describedPaths(): Set<string>`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/guide/coverage.test.ts`:

```typescript
import { describe, it, expect } from 'vitest'
import { routeTable } from '../router'
import { flattenRoutePaths, isDocumentable } from './coverage'
import { SURFACES } from './surfaces'

const declared = flattenRoutePaths(routeTable).filter(isDocumentable)
const described = new Set(SURFACES.map((s) => s.path))

describe('guide coverage', () => {
  it('finds the real routes', () => {
    // A sanity floor: if the flattener silently returns nothing, both
    // directions below pass vacuously and the test proves nothing.
    expect(declared.length).toBeGreaterThan(20)
    expect(declared).toContain('/settings')
    expect(declared).toContain('/w/:workspace/agents')
  })

  it('documents every documentable route', () => {
    const missing = declared.filter((p) => !described.has(p))
    expect(missing, `routes with no descriptor in guide/surfaces.ts: ${missing.join(', ')}`).toEqual([])
  })

  it('has no descriptor for a route that does not exist', () => {
    // The direction that catches a descriptor orphaned by a deleted route —
    // which is how a generated guide starts lying.
    const declaredSet = new Set(declared)
    const orphans = [...described].filter((p) => !declaredSet.has(p))
    expect(orphans, `descriptors naming no real route: ${orphans.join(', ')}`).toEqual([])
  })

  it('gives every descriptor the fields the guide renders', () => {
    for (const s of SURFACES) {
      expect(s.title, `${s.path} has no title`).toBeTruthy()
      expect(s.what, `${s.path} has no 'what'`).toBeTruthy()
      expect(s.audience, `${s.path} has no audience`).toBeTruthy()
    }
  })
})

describe('isDocumentable', () => {
  it('excludes the catch-all and legacy aliases', () => {
    expect(isDocumentable('*')).toBe(false)
    expect(isDocumentable('/ddd-plans')).toBe(false)
    expect(isDocumentable('/w/:workspace/agents/:slug/needs-you')).toBe(false)
  })

  it('includes real surfaces', () => {
    expect(isDocumentable('/settings')).toBe(true)
    expect(isDocumentable('/w/:workspace/chat/:id')).toBe(true)
  })
})
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `cd frontend && npx vitest run src/guide/coverage.test.ts`
Expected: FAIL — cannot resolve `./coverage`.

- [ ] **Step 3: Write the coverage helpers**

Create `frontend/src/guide/coverage.ts`:

```typescript
import type { RouteObject } from 'react-router-dom'

/**
 * Flatten the route table into absolute path strings, joining child paths onto
 * their parent (the agent workspace's rail sections are children).
 */
export function flattenRoutePaths(routes: RouteObject[], parent = ''): string[] {
  const out: string[] = []
  for (const r of routes) {
    const raw = (r as { path?: string }).path
    const here =
      raw === undefined
        ? parent
        : raw.startsWith('/')
          ? raw
          : `${parent.replace(/\/$/, '')}/${raw}`
    if (raw !== undefined) out.push(here)
    const kids = (r as { children?: RouteObject[] }).children
    if (kids?.length) out.push(...flattenRoutePaths(kids, here))
  }
  return out
}

/**
 * Paths that need no descriptor: the catch-all, and anything whose only job is
 * to send the browser somewhere else. These are listed explicitly rather than
 * detected from the element, because an explicit list fails LOUDLY when a new
 * redirect is added (the coverage test names it) instead of silently excusing it.
 */
const NOT_DOCUMENTABLE = new Set([
  '*',
  '/',
  '/timeline',
  '/shareouts/*',
  '/walkthroughs',
  '/storyboards',
  '/agents/*',
  '/ddd/*',
  '/ddd-plans',
  '/reviews',
  '/w/:workspace/agents/:slug/needs-you',
  '/w/:workspace/agents/:slug',
])

export function isDocumentable(path: string): boolean {
  if (NOT_DOCUMENTABLE.has(path)) return false
  if (path.endsWith('/*')) return false
  return true
}
```

Note on `/w/:workspace/agents/:slug`: it is the rail *shell* whose index redirects to
`inbox`; the documented surfaces are its children. `/` is excluded because it is the
root redirect — the first-run experience it now shows is documented as a path in
`paths.ts`, not as a route descriptor.

- [ ] **Step 4: Run the test to see the real list of paths needing descriptors**

Run: `cd frontend && npx vitest run src/guide/coverage.test.ts`
Expected: FAIL on `documents every documentable route`, with the failure message listing
every path. **Copy that list** — it is the exact set of descriptors to write next.

- [ ] **Step 5: Write the descriptor registry**

Create `frontend/src/guide/surfaces.ts`. **Step 4's failure message is the authoritative
list of paths** — it names every one that needs a descriptor. The six entries below
establish the shape and the voice; write the rest from what each page actually does,
reading the component where the path alone is not enough. You cannot finish this step
early: the coverage test stays red, naming each gap, until every path is covered.

```typescript
/**
 * One descriptor per documented surface — the in-app guide's content, as DATA.
 *
 * Data, not prose in a wiki, for the same reason systemWorkflows.ts is data: a
 * test can read this and a page can render it; prose in another repo can do
 * neither. guide/coverage.test.ts asserts this file covers every route and
 * names no route that does not exist.
 *
 * `path` MUST match the route path in router.tsx character for character.
 */
export interface SurfaceDescriptor {
  /** Exactly the route path from routeTable. */
  path: string
  title: string
  /** Who this is for, in a few words. */
  audience: string
  /** What it is, one line. */
  what: string
  /** What you need before it does anything useful. */
  needsFirst?: string
  /** What you can actually do here. */
  actions?: string[]
}

export const SURFACES: SurfaceDescriptor[] = [
  {
    path: '/w/:workspace',
    title: 'Project workbench',
    audience: 'Anyone in a workspace',
    what: 'The workspace home — a tile per project, ranked by how many insights are waiting on it, with the top three surfaced as a hero.',
    actions: ['Triage an insight inline', 'Open a project'],
  },
  {
    path: '/w/:workspace/chat',
    title: 'Chats',
    audience: 'An operator who wants work done',
    what: 'Your chat sessions with agents, so you can pick one up from any device.',
    needsFirst: 'An agent in this workspace, and a runner online to execute its turns.',
    actions: ['Start a new chat with an agent', 'Continue an existing session'],
  },
  {
    path: '/w/:workspace/chat/:id',
    title: 'One chat',
    audience: 'An operator',
    what: 'A live multiplayer chat with an agent. Sending enqueues a turn; the reply streams back as the agent works.',
    needsFirst: 'A runner online and assigned to this agent.',
    actions: ['Send a message', 'Answer a question the agent is blocked on', 'Move the session to another runner'],
  },
  {
    path: '/supervisor',
    title: 'Supervisor',
    audience: 'Someone who owns agents',
    what: 'The cross-fleet "waiting on you" inbox, agent KPI cards and runner status. Spans every workspace, which is why it is not tenant-scoped.',
    needsFirst: 'At least one agent you own.',
    actions: ['Answer a blocked agent', 'See which runners are online', 'Install it as a phone app and get pushed when an agent needs you'],
  },
  {
    path: '/settings',
    title: 'Settings',
    audience: 'Everyone',
    what: 'Your account and this deployment: which AI backend is in use, your Claude subscription connection, presence visibility, and your personal access tokens.',
    actions: ['Mint a personal access token', 'Revoke a token', 'Switch AI backend', 'Toggle presence'],
  },
  {
    path: '/system',
    title: 'Capabilities',
    audience: 'Anyone deciding what canopy can do',
    what: "Every skill, agent and command the canopy plugin ships, read live from the plugin's own files, plus curated chains showing how they compose.",
    actions: ['Browse the catalog', 'Read a capability in full'],
  },
  // Every remaining path printed by Step 4 gets an entry in this same shape.
  // This is not left to judgement about WHICH ones: the coverage test fails,
  // naming each missing path, until the list is complete — so "done" is a test
  // result, not an opinion. Write the prose from what the page actually does;
  // read the component if you are unsure rather than guessing from the path.
]

export function describedPaths(): Set<string> {
  return new Set(SURFACES.map((s) => s.path))
}
```

- [ ] **Step 6: Run the coverage test until it is green**

Run: `cd frontend && npx vitest run src/guide/coverage.test.ts`
Expected: all pass. Iterate: each failure names exactly which path is missing a
descriptor or which descriptor names no route.

- [ ] **Step 7: Confirm it is fast**

Run: `cd frontend && npx vitest run src/guide/coverage.test.ts --reporter=verbose`
Expected: the file's duration is milliseconds, not seconds. If it is slow, something is
importing React components transitively — the test must only pull in `routeTable`'s
*paths*. If `router.tsx`'s lazy imports make the import heavy, move `routeTable` into its
own module (`frontend/src/routeTable.ts`) that holds paths and element references only.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/guide/coverage.ts frontend/src/guide/surfaces.ts frontend/src/guide/coverage.test.ts
git commit -m "guide: descriptor registry with two-direction coverage

Every documented route must have a descriptor and every descriptor must
name a real route. The second direction is what catches a descriptor
orphaned by a deleted route, which is how a generated guide starts lying."
```

---

## Task 7: The five-ways-in registry

Shared by `/guide` and the public explainer so the public promise cannot drift from the
internal documentation. This is the spec's only deliberate coupling between the two pages.

**Files:**
- Create: `frontend/src/guide/paths.ts`
- Create: `frontend/src/guide/paths.test.ts`

**Interfaces:**
- Consumes: `SURFACES` (Task 6) for cross-referencing.
- Produces: `USER_PATHS: UserPath[]` where
  `UserPath = { id: string; title: string; who: string; startHere: string; surfaces: string[]; note?: string }`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/guide/paths.test.ts`:

```typescript
import { describe, it, expect } from 'vitest'
import { USER_PATHS } from './paths'
import { describedPaths } from './surfaces'

describe('USER_PATHS', () => {
  it('describes the five ways in', () => {
    expect(USER_PATHS).toHaveLength(5)
  })

  it('has unique ids', () => {
    const ids = USER_PATHS.map((p) => p.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('only references surfaces that are actually documented', () => {
    // The coupling that keeps the public page honest: it cannot promise a
    // surface the guide does not describe.
    const known = describedPaths()
    const dangling = USER_PATHS.flatMap((p) =>
      p.surfaces.filter((s) => s.startsWith('/') && !known.has(s)),
    )
    expect(dangling, `paths referencing undocumented surfaces: ${dangling.join(', ')}`).toEqual([])
  })

  it('tells every path where to start', () => {
    for (const p of USER_PATHS) {
      expect(p.startHere, `${p.id} has no starting point`).toBeTruthy()
      expect(p.who, `${p.id} does not say who it is for`).toBeTruthy()
    }
  })
})
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `cd frontend && npx vitest run src/guide/paths.test.ts`
Expected: FAIL — cannot resolve `./paths`.

- [ ] **Step 3: Write the registry**

Create `frontend/src/guide/paths.ts`:

```typescript
/**
 * The five ways into canopy. Rendered by BOTH the public explainer and the
 * in-app guide, so the public promise cannot drift from the internal docs —
 * paths.test.ts asserts every referenced surface is one the guide describes.
 *
 * Two notes on this taxonomy, because it differs from the obvious one.
 * "Watch a turn in the browser" and "watch a turn on your laptop" are NOT two
 * paths: they are one job with a deployment choice inside it, and the choice is
 * the interesting part. And "you were sent a link" is first, because it is
 * almost certainly the highest-volume interaction anyone has with canopy.
 */
export interface UserPath {
  id: string
  title: string
  /** Who you are, if this is your path. */
  who: string
  /** The literal first thing to do. */
  startHere: string
  /** Route paths (from surfaces.ts) or external entry points. */
  surfaces: string[]
  note?: string
}

export const USER_PATHS: UserPath[] = [
  {
    id: 'audience',
    title: 'You were sent a link',
    who: 'An audience. No login, no install, nothing to set up.',
    startHere: 'Open the link. That is the whole path.',
    surfaces: ['/storyboard/:slug', '/narrative/:slug', '/walkthrough/:id', '/share/:token'],
    note: 'Storyboards, narratives, demo walkthroughs and shared transcripts are all public when shared.',
  },
  {
    id: 'operator',
    title: 'You want an agent to do work',
    who: 'An operator with a job to hand off.',
    startHere: 'Open Chats in a workspace and start a chat with an agent.',
    surfaces: ['/w/:workspace/chat', '/w/:workspace/chat/:id', '/w/:workspace/activity'],
    note: 'Then choose where it runs: a cloud runner needs nothing from you, or pair your own laptop so you can jump into the terminal mid-turn.',
  },
  {
    id: 'supervisor',
    title: 'You keep agents unblocked',
    who: 'Someone who owns agents and is not driving every turn.',
    startHere: 'Open Supervisor, then install it as a phone app.',
    surfaces: ['/supervisor', '/schedules'],
    note: 'It pushes a notification when any agent you own starts waiting on you.',
  },
  {
    id: 'skills',
    title: "You want canopy's skills in your own sessions",
    who: 'A Claude Code user who does not want to run a fleet.',
    startHere: 'Mint a token in Settings, then install the canopy plugin.',
    surfaces: ['/settings', '/system'],
    note: 'No agents, no runners. Just the capability library in your own Claude Code.',
  },
  {
    id: 'builder',
    title: 'You want to build an agent',
    who: 'A builder who needs a persona of their own.',
    startHere: 'Run /canopy:create-agent, then register it in the workspace.',
    surfaces: ['/w/:workspace/agents'],
    note: 'The scaffold is a skeleton — persona and domain skills are yours to write.',
  },
]
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `cd frontend && npx vitest run src/guide/paths.test.ts`
Expected: all pass. If `only references surfaces that are actually documented` fails, the
named surface is missing from `surfaces.ts` — add its descriptor rather than deleting the
reference.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/guide/paths.ts frontend/src/guide/paths.test.ts
git commit -m "guide: the five ways in, as a shared registry

Consumed by both the public explainer and the in-app guide, with a test
asserting it never promises a surface the guide does not describe."
```

---

## Task 8: The `/guide` page

**Files:**
- Create: `frontend/src/pages/GuidePage.tsx`
- Modify: `frontend/src/router.tsx` (add the route)
- Modify: `frontend/src/components/AppLayout/nav.ts` (add the nav entry)

**Interfaces:**
- Consumes: `SURFACES` (Task 6), `USER_PATHS` (Task 7), `NAV_GROUPS` from
  `@/components/AppLayout/nav`.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the grouping helper and its test**

Create `frontend/src/guide/grouping.ts`:

```typescript
import { NAV_GROUPS } from '@/components/AppLayout/nav'
import { SURFACES, type SurfaceDescriptor } from './surfaces'

/**
 * Group descriptors by the header's existing nav groups, so the guide's shape
 * matches the menu people actually look at. Anything the nav does not mention
 * (public viewers, agent rail sections) lands in a trailing "Elsewhere" group
 * rather than being dropped — a guide that silently omits a surface is the
 * failure this whole registry exists to prevent.
 */
export interface GuideGroup {
  label: string
  surfaces: SurfaceDescriptor[]
}

/** The route path a nav item points at, in routeTable's notation. */
function navItemPath(item: { path: string; tenant: boolean }): string {
  if (!item.tenant) return item.path
  return item.path ? `/w/:workspace/${item.path}` : '/w/:workspace'
}

export function guideGroups(surfaces: SurfaceDescriptor[] = SURFACES): GuideGroup[] {
  const claimed = new Set<string>()
  const groups: GuideGroup[] = []

  for (const nav of NAV_GROUPS) {
    const wanted = nav.items.map(navItemPath)
    const members = surfaces.filter((s) => wanted.includes(s.path))
    members.forEach((m) => claimed.add(m.path))
    if (members.length) groups.push({ label: nav.label, surfaces: members })
  }

  const rest = surfaces.filter((s) => !claimed.has(s.path))
  if (rest.length) groups.push({ label: 'Elsewhere', surfaces: rest })
  return groups
}
```

Create `frontend/src/guide/grouping.test.ts`:

```typescript
import { describe, it, expect } from 'vitest'
import { guideGroups } from './grouping'
import { SURFACES } from './surfaces'

describe('guideGroups', () => {
  it('places every surface in exactly one group', () => {
    const grouped = guideGroups().flatMap((g) => g.surfaces.map((s) => s.path))
    expect(grouped.length).toBe(SURFACES.length)
    expect(new Set(grouped).size).toBe(SURFACES.length)
  })

  it('uses the nav group labels', () => {
    const labels = guideGroups().map((g) => g.label)
    expect(labels).toContain('Work')
    expect(labels).toContain('Fleet')
  })

  it('never drops a surface the nav does not mention', () => {
    const groups = guideGroups([
      { path: '/share/:token', title: 'Shared session', audience: 'Anyone', what: 'x' },
    ])
    expect(groups).toEqual([
      { label: 'Elsewhere', surfaces: [expect.objectContaining({ path: '/share/:token' })] },
    ])
  })
})
```

- [ ] **Step 2: Run and confirm it fails, then passes**

Run: `cd frontend && npx vitest run src/guide/grouping.test.ts`
Expected: FAIL (unresolved import) before Step 1's file exists; PASS after.

- [ ] **Step 3: Write the page**

Create `frontend/src/pages/GuidePage.tsx`:

```tsx
import { guideGroups } from '@/guide/grouping'
import { USER_PATHS } from '@/guide/paths'

/**
 * The in-app guide: what every surface is, generated from guide/surfaces.ts and
 * grouped by the header nav. A coverage test guarantees nothing is missing;
 * empty states deep-link here with #<route-path> anchors, so the stuck-user
 * surface and the documentation surface are the same content.
 */
export function GuidePage() {
  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <h1 className="text-lg font-semibold text-foreground">Guide</h1>
      <p className="mt-2 text-[13px] leading-relaxed text-foreground-secondary">
        What each part of Canopy is for. Generated from the app&apos;s own route table, so
        it cannot quietly miss a page.
      </p>

      <section className="mt-8">
        <h2 className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Start here
        </h2>
        <ol className="mt-3 space-y-3">
          {USER_PATHS.map((p, i) => (
            <li key={p.id} className="flex gap-3 rounded-lg border border-border bg-card p-3">
              <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-muted text-[10px] font-semibold text-muted-foreground">
                {i + 1}
              </span>
              <div>
                <h3 className="text-sm font-semibold text-foreground">{p.title}</h3>
                <p className="mt-0.5 text-[12px] text-muted-foreground">{p.who}</p>
                <p className="mt-1.5 text-[13px] text-foreground-secondary">{p.startHere}</p>
                {p.note ? <p className="mt-1 text-[12px] text-foreground-subtle">{p.note}</p> : null}
              </div>
            </li>
          ))}
        </ol>
      </section>

      {guideGroups().map((group) => (
        <section key={group.label} className="mt-8">
          <h2 className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            {group.label}
          </h2>
          <div className="mt-3 space-y-3">
            {group.surfaces.map((s) => (
              <article
                key={s.path}
                id={s.path}
                className="scroll-mt-20 rounded-lg border border-border bg-card p-4"
              >
                <div className="flex items-baseline justify-between gap-3">
                  <h3 className="text-sm font-semibold text-foreground">{s.title}</h3>
                  <code className="shrink-0 text-[11px] text-foreground-subtle">{s.path}</code>
                </div>
                <p className="mt-1 text-[12px] text-muted-foreground">{s.audience}</p>
                <p className="mt-2 text-[13px] leading-relaxed text-foreground-secondary">{s.what}</p>
                {s.needsFirst ? (
                  <p className="mt-2 text-[12px] text-warning">Needs first: {s.needsFirst}</p>
                ) : null}
                {s.actions?.length ? (
                  <ul className="mt-2 list-disc space-y-0.5 pl-5 text-[12px] text-foreground-secondary">
                    {s.actions.map((a) => (
                      <li key={a}>{a}</li>
                    ))}
                  </ul>
                ) : null}
              </article>
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}
```

- [ ] **Step 4: Add the route and the nav entry**

In `frontend/src/router.tsx`, import `GuidePage` and add beside the other global routes:

```tsx
      { path: '/guide', element: <GuidePage /> },
```

Then add its descriptor to `frontend/src/guide/surfaces.ts` (the coverage test will
demand it):

```typescript
  {
    path: '/guide',
    title: 'Guide',
    audience: 'Anyone finding their way around',
    what: "What every surface in Canopy is for, generated from the app's own route table.",
    actions: ['Find the page you need', 'See what a surface needs before it works'],
  },
```

In `frontend/src/components/AppLayout/nav.ts`, add it to the last group (the
team/admin one), so it is reachable without knowing the URL.

- [ ] **Step 5: Run the whole frontend suite and build**

Run: `cd frontend && npm run test && npm run build`
Expected: all pass. The coverage test now includes `/guide` itself.

- [ ] **Step 6: Verify by rendering the page**

Open `/guide` in the running app. Confirm: the five paths appear numbered at the top,
groups match the header menus, and a deep link works — `/guide#/settings` should scroll
to the Settings entry.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/guide/grouping.ts frontend/src/guide/grouping.test.ts frontend/src/pages/GuidePage.tsx frontend/src/guide/surfaces.ts frontend/src/router.tsx frontend/src/components/AppLayout/nav.ts
git commit -m "guide: /guide renders the registry grouped by the nav

Grouped by NAV_GROUPS so the guide's shape matches the menu people look at,
with anything the nav omits landing in a trailing group rather than being
dropped."
```

---

## Task 9: Empty states link into the guide

Per the spec, the stuck-user surface and the documentation surface should be the same
content. The agents empty state also *teaches* the create-agent flow rather than offering
a button, because a web-created agent has no repo and cannot take a turn until the
companion spec ships.

**Files:**
- Modify: `frontend/src/pages/AgentsPage.tsx`
- Modify: `frontend/src/pages/ChatListPage.tsx`

**Use react-router `<Link to="/guide#...">`, never a relative `<a href>`.** A relative
href resolves differently at each route depth and ignores the `/canopy` basename: from
`/w/:ws/chat`, `../guide` lands on `/w/guide`. `Link` with an absolute path handles the
basename automatically and is depth-independent. Add
`import { Link } from 'react-router-dom'` to each file if it is not already there.

**Interfaces:**
- Consumes: `USER_PATHS` (Task 7) for the copy's starting points.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Read both pages to find the current empty rendering**

Run:

```bash
grep -n "length === 0\|No agents\|empty\|length ? " frontend/src/pages/AgentsPage.tsx frontend/src/pages/ChatListPage.tsx
```

Note what each renders when its list is empty. If a page has no empty branch at all, add
one — an empty table with headers and no rows is the same dead end in a smaller frame.

- [ ] **Step 2: Add the agents empty state**

In `frontend/src/pages/AgentsPage.tsx`, where the list is empty:

```tsx
<div className="rounded-lg border border-border bg-card p-4">
  <h2 className="text-sm font-semibold text-foreground">No agents in this workspace</h2>
  <p className="mt-2 text-[13px] leading-relaxed text-foreground-secondary">
    An agent is a persona with its own git repo — skills, hooks and an identity. It is
    not created here: run <code className="text-primary">/canopy:create-agent</code> in
    Claude Code, which scaffolds the repo, then it appears in this list.
  </p>
  <p className="mt-2 text-[12px] text-muted-foreground">
    You will also need a runner online to execute its turns.{' '}
    <Link to="/guide#/w/:workspace/agents" className="text-primary hover:underline">
      Read more in the guide
    </Link>
    .
  </p>
</div>
```

Do **not** add a create button. Until GitHub-backed creation ships, `POST /api/agents/`
produces a record with no repo, no persona and no skills — an agent that cannot take a
turn, which is worse than no button. See
`docs/superpowers/specs/2026-09-12-github-backed-agent-creation-design.md`.

- [ ] **Step 3: Add the chats empty state**

In `frontend/src/pages/ChatListPage.tsx`, where the session list is empty:

```tsx
<div className="rounded-lg border border-border bg-card p-4">
  <h2 className="text-sm font-semibold text-foreground">No chats yet</h2>
  <p className="mt-2 text-[13px] leading-relaxed text-foreground-secondary">
    Start a chat to hand an agent a job. Sending a message enqueues a turn, and a runner
    picks it up and streams the reply back as it works.
  </p>
  <p className="mt-2 text-[12px] text-muted-foreground">
    Nothing to chat with?{' '}
    <Link to="/guide#/w/:workspace/agents" className="text-primary hover:underline">
      You need an agent first
    </Link>
    .
  </p>
</div>
```

- [ ] **Step 4: Build and run the suite**

Run: `cd frontend && npm run build && npm run test`
Expected: tsc clean, tests pass.

- [ ] **Step 5: Verify the links resolve**

In the running app, open a workspace with no agents, click through to the guide from the
empty state, and confirm the address bar shows `/canopy/guide#/w/:workspace/agents` (not
`/canopy/w/<ws>/guide...`) and that the page scrolls to the Agents entry. Do the same from
an empty Chats list — the two pages sit at different route depths, which is exactly what
a relative href would get wrong.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/AgentsPage.tsx frontend/src/pages/ChatListPage.tsx
git commit -m "ui: empty states teach and link into the guide

The agents empty state deliberately hands over /canopy:create-agent rather
than offering a button: until GitHub-backed creation ships, a web-created
agent has no repo and cannot take a turn."
```

---

## Task 10: `GET /api/system/public-stats`

Anonymous, aggregates only. **Two** changes are needed for anything to be public here:
`auth=None` on the route, and an allowlist entry in `apps/common/middleware.py` — the
login middleware is default-deny, so `auth=None` alone still bounces an anonymous caller
to Google.

**Files:**
- Create: `apps/system/stats.py`
- Modify: `apps/system/schemas.py`
- Modify: `apps/system/api.py`
- Modify: `apps/common/middleware.py` (`PUBLIC_PATH_PREFIXES`)
- Test: `tests/test_public_stats.py`

**Interfaces:**
- Consumes: `apps.agents.models.Agent`, `apps.harness.models.Runner`,
  `apps.harness.models.Turn`, `apps.runs` release models, and
  `apps.system.reader.load_catalog` for the skill count.
- Produces: `apps.system.stats.public_stats() -> dict` with keys
  `agents`, `skills`, `runners_online`, `turns_executed`, `demos_published` (all `int`),
  and `PublicStatsOut` with exactly those fields.

- [ ] **Step 1: Read the three facts this task depends on**

These were verified while writing the plan. Read them yourself before coding — two of
them will crash your code if you assume the obvious thing instead.

1. **`Runner.live_status` is a computed `@property`, not a database column**
   (`apps/harness/models.py:255-272`). It derives from `status`, `paused` and
   `last_heartbeat_at` against `HEARTBEAT_ONLINE_WINDOW` (90 seconds, line 20). So
   `Runner.objects.filter(live_status=Runner.ONLINE)` raises `FieldError`. The
   DB-expressible equivalent of "online" is a fresh heartbeat on a non-retired runner.
2. **`Turn.DONE == "done"`** (`apps/harness/models.py:301`), and `Turn.TERMINAL` is the
   set of finished states. Count `DONE` only — "turns executed" should not include
   failures and cancellations.
3. **`apps/runs` has no `models.py` at all.** DDD runs are a *derived read model* over
   `apps.walkthroughs.models.Walkthrough` (grouped by `run_id`) and
   `apps.reviews.models.ReviewRequest` — see `apps/runs/aggregate.py:17-23`. There is no
   "published demo" row to count. `Walkthrough.run_id` is the run grouping key and is
   indexed (`apps/walkthroughs/models.py:41`, `:101`), so the honest metric is the number
   of distinct non-empty `run_id` values.

Confirm each for yourself:

4. **`apps/system` is FRAMEWORK and `apps/walkthroughs` is PRODUCT**
   (`tests/test_architecture_boundary.py:27-28`). Framework code must never import
   product code — and the test blocks even a **string** reference to a product module.
   So `apps/system/stats.py` **cannot** import `Walkthrough`. The demos count is
   contributed by the product app through a registry instead, which is the pattern
   `apps/timeline` already uses to aggregate product events without importing product
   (`apps/timeline/sources.py:25`). Getting this wrong fails CI, not just review.

```bash
sed -n 255,272p apps/harness/models.py      # live_status is a property
grep -n 'DONE, FAILED' apps/harness/models.py
ls apps/runs/                                # no models.py
grep -n 'run_id' apps/walkthroughs/models.py
grep -n 'FRAMEWORK = \|PRODUCT = ' tests/test_architecture_boundary.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_public_stats.py`:

```python
"""GET /api/system/public-stats — anonymous, aggregates only.

The leak assertion is the load-bearing one. An anonymous endpoint on a
multi-tenant system is exactly where a field gets added later without anyone
re-asking whether it should be public, so the test asserts the SHAPE is closed
rather than only that today's fields are fine.
"""
import pytest

ALLOWED_KEYS = {
    "agents",
    "skills",
    "runners_online",
    "turns_executed",
    "demos_published",
}


def test_reachable_without_authentication(client, db):
    resp = client.get("/api/system/public-stats")
    assert resp.status_code == 200, resp.content


def test_returns_only_integer_aggregates(client, db):
    body = client.get("/api/system/public-stats").json()
    assert set(body) == ALLOWED_KEYS
    for key, value in body.items():
        assert isinstance(value, int), f"{key} is {type(value)}, not an int"


def test_leaks_no_names_slugs_or_ids(client, db):
    # A count cannot leak what it is a count of. Anything that is not an int
    # could — so the guard is on the type, not on a denylist of field names.
    body = client.get("/api/system/public-stats").json()
    assert not any(isinstance(v, (str, list, dict)) for v in body.values())


def test_counts_reflect_reality(client, db):
    from apps.agents.models import Agent
    from apps.workspaces.models import Workspace

    ws = Workspace.objects.create(slug="acme", display_name="Acme")
    Agent.objects.create(slug="a1", display_name="A1", workspace=ws)
    Agent.objects.create(slug="a2", display_name="A2", workspace=ws)

    body = client.get("/api/system/public-stats").json()
    assert body["agents"] == 2
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `uv run pytest tests/test_public_stats.py -v`
Expected: FAIL — 404 or a redirect to login on every test.

- [ ] **Step 4: Write the stats module**

Create `apps/system/stats.py`:

```python
"""Aggregate counts for the public explainer page.

Aggregates ONLY — no names, no slugs, no ids, no per-agent or per-tenant
breakdown, no content. A count cannot leak what it is a count of, which is the
whole reason this is safe to serve anonymously. Adding a non-integer field here
is a security change, and tests/test_public_stats.py fails on the type rather
than on a denylist of names so that it cannot be done by accident.

No Django request object — pure functions over the ORM, so they are testable
directly.
"""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import HEARTBEAT_ONLINE_WINDOW, Runner, Turn

from . import reader

#: Product apps contribute counts here from their AppConfig.ready(). This app is
#: FRAMEWORK and must not import PRODUCT (tests/test_architecture_boundary.py),
#: so the arrow has to point inward: product names framework, never the reverse.
#: Same reason apps/timeline reads product events through a registry.
_EXTRA: dict[str, "Callable[[], int]"] = {}


def register_extra_stat(name: str, fn: "Callable[[], int]") -> None:
    """Register a product-owned count. Idempotent — re-registering replaces."""
    _EXTRA[name] = fn


def _skill_count() -> int:
    try:
        cat = reader.load_catalog(settings.CANOPY_PLUGIN_PATH)
    except Exception:  # noqa: BLE001 — a missing plugin path must not 500 a public page
        return 0
    return int(cat.get("counts", {}).get("skill", 0))


def _runners_online() -> int:
    """Runners with a fresh heartbeat, excluding retired boxes.

    `Runner.live_status` is a PROPERTY, not a column, so it cannot be filtered
    on. This is its DB-expressible core: the same 90-second window
    (HEARTBEAT_ONLINE_WINDOW) that live_status uses to demote a runner to STALE.
    Deliberately NOT `is_available` — readiness is an operational detail and
    "how many boxes are up" is the honest public number.
    """
    cutoff = timezone.now() - HEARTBEAT_ONLINE_WINDOW
    return (
        Runner.objects.exclude(status=Runner.RETIRED)
        .filter(last_heartbeat_at__gte=cutoff)
        .count()
    )


def _extra(name: str) -> int:
    """A registered product count, or 0 if that app did not register one.

    Defaulting to 0 rather than omitting the key keeps PublicStatsOut's field set
    CLOSED, which is what the leak test asserts — a dynamic response shape would
    make "only integers, only these keys" unenforceable.
    """
    fn = _EXTRA.get(name)
    if fn is None:
        return 0
    try:
        return int(fn())
    except Exception:  # noqa: BLE001 — one bad contributor must not 500 a public page
        return 0


def public_stats() -> dict[str, int]:
    return {
        "agents": Agent.objects.count(),
        "skills": _skill_count(),
        "runners_online": _runners_online(),
        "turns_executed": Turn.objects.filter(status=Turn.DONE).count(),
        "demos_published": _extra("demos_published"),
    }
```

Add `from collections.abc import Callable` to the imports (used in the annotations above).

Then register the product-owned count from the **product** side, in
`apps/walkthroughs/apps.py`:

```python
class WalkthroughsConfig(AppConfig):
    name = "apps.walkthroughs"

    def ready(self) -> None:
        # Contribute the published-demo count to the framework's public stats.
        # Registered from HERE, not imported from there: apps/system is FRAMEWORK
        # and must not name a PRODUCT module (tests/test_architecture_boundary.py).
        # In ready() so it runs once, after the app registry is populated.
        from apps.system.stats import register_extra_stat

        from .models import Walkthrough

        def demos_published() -> int:
            # A "run package" is a derived grouping of Walkthrough rows by
            # run_id (apps/runs/aggregate.py) — apps/runs has no models. Counting
            # rows would inflate it: one package is a video AND a deck.
            return (
                Walkthrough.objects.exclude(run_id__isnull=True)
                .exclude(run_id="")
                .values("run_id")
                .distinct()
                .count()
            )

        register_extra_stat("demos_published", demos_published)
```

Keep whatever `AppConfig` attributes the existing file already sets (`default_auto_field`,
`label`, `verbose_name`) — only the `ready()` method is being added.

- [ ] **Step 5: Add the schema**

In `apps/system/schemas.py`:

```python
class PublicStatsOut(StrictModel):
    """Aggregates for the public explainer. Integers only — see apps/system/stats.py."""

    agents: int
    skills: int
    runners_online: int
    turns_executed: int
    demos_published: int
```

- [ ] **Step 6: Add the route**

In `apps/system/api.py`, import the new pieces and add the route **above** the
`/{kind}/{name}` catch-all — otherwise `public-stats` is captured as a `kind`:

```python
from . import reader, stats
from .schemas import CapabilityCatalogOut, CapabilityDetailOut, PublicStatsOut
```

```python
@router.get(
    "/public-stats",
    response=PublicStatsOut,
    auth=None,
    summary="Aggregate counts for the public explainer (anonymous)",
)
def public_stats(request: HttpRequest) -> dict:
    """Anonymous, aggregates only.

    NOTE: `auth=None` is half the story — apps/common/middleware.py is
    default-deny, so this path is also allowlisted there. Both are required.
    """
    return stats.public_stats()
```

Placement matters: `detail()` matches `/{kind}/{name}`, and Ninja resolves in
declaration order.

- [ ] **Step 7: Allowlist the path in the login middleware**

In `apps/common/middleware.py`, add to `PUBLIC_PATH_PREFIXES`:

```python
    "/api/system/public-stats",  # auth=None — aggregates only, no names/ids (public explainer)
```

- [ ] **Step 8: Run the test and the architecture boundary test**

Run:

```bash
uv run pytest tests/test_public_stats.py tests/test_architecture_boundary.py -v
```

Expected: 4 passed for the stats, and the boundary tests green. If
`test_framework_apps_do_not_import_product` or
`test_framework_source_does_not_reference_product_modules` fails, `apps/system/stats.py`
is still naming `apps.walkthroughs` — the count must come through
`register_extra_stat`, called from the walkthroughs app, not from an import here.

Add one more test to `tests/test_public_stats.py` covering the registry, since a
contributor that silently fails to register would report 0 forever:

```python
def test_demos_published_comes_from_the_registry(client, db):
    from django.contrib.auth import get_user_model

    from apps.walkthroughs.models import Walkthrough

    owner = get_user_model().objects.create_user(username="o", email="o@dimagi.com")

    def artifact(kind: str) -> None:
        # Walkthrough has SEVEN fields with neither null/blank nor a default
        # (verified against apps/walkthroughs/models.py): title, kind, owner,
        # drive_file_id, drive_folder_id, content_type, size_bytes. Omitting any
        # of them raises IntegrityError, not a validation error.
        Walkthrough.objects.create(
            title=f"artifact-{kind}",
            kind=kind,
            owner=owner,
            drive_file_id=f"file-{kind}",
            drive_folder_id="folder-1",
            content_type="video/mp4" if kind == "video" else "text/html",
            size_bytes=1,
            run_id="demo-2026-09-12-001",
        )

    # Two artifacts, ONE run package — the count must be 1, not 2. This is the
    # assertion that catches counting rows instead of distinct run_ids.
    artifact("video")
    artifact("html")

    body = client.get("/api/system/public-stats").json()
    assert body["demos_published"] == 1


def test_demos_published_ignores_artifacts_with_no_run(client, db):
    from django.contrib.auth import get_user_model

    from apps.walkthroughs.models import Walkthrough

    owner = get_user_model().objects.create_user(username="o2", email="o2@dimagi.com")
    # A one-off upload carries no run_id — it is not a published package.
    Walkthrough.objects.create(
        title="loose", kind="html", owner=owner, drive_file_id="f2",
        drive_folder_id="folder-1", content_type="text/html", size_bytes=1,
    )

    body = client.get("/api/system/public-stats").json()
    assert body["demos_published"] == 0
```

`kind` must be one of `Walkthrough.KIND_CHOICES` (`apps/walkthroughs/models.py:34`) —
check the valid values before inventing one.

- [ ] **Step 9: Confirm it is genuinely anonymous end to end**

Run, with the backend up and **no** session cookie:

```bash
curl -sS -i http://localhost:8000/api/system/public-stats | head -20
```

Expected: `HTTP/1.1 200` and a JSON body of five integers. A `302` to Google means the
middleware allowlist entry is missing or mis-spelled; a `401` means `auth=None` did not
take.

- [ ] **Step 10: Regenerate the frontend types and run the backend suite**

```bash
cd frontend && npm run gen:api
cd .. && uv run pytest -q
```

Expected: `PublicStatsOut` appears in `generated.ts`; no new backend failures.

- [ ] **Step 11: Commit**

```bash
git add apps/system/stats.py apps/system/schemas.py apps/system/api.py apps/walkthroughs/apps.py apps/common/middleware.py tests/test_public_stats.py frontend/src/api/generated.ts
git commit -m "api: anonymous aggregate stats for the public explainer

Integers only, and the test asserts the shape is closed by TYPE rather than
by a denylist of field names — an anonymous endpoint on a multi-tenant
system is exactly where a leaky field gets added later by accident.

Public needs two changes, not one: auth=None on the route AND an allowlist
entry, because the login middleware is default-deny."
```

---

## Task 11: The public explainer page

Chrome-less, anonymous, on `PublicLayout` — mounted **outside** `AppLayout` so the
shell's authed calls do not bounce an anonymous visitor to login. The SPA route also
needs the PWA navigate-fallback allowlist and the login middleware's page allowlist.

**Files:**
- Create: `frontend/src/api/publicStats.ts`
- Create: `frontend/src/guide/components.ts`
- Create: `frontend/src/pages/AboutPage.tsx`
- Modify: `frontend/src/router.tsx`
- Modify: `frontend/src/pwa/navigation-fallback.ts`
- Modify: `apps/common/middleware.py`

**Interfaces:**
- Consumes: `USER_PATHS` (Task 7), `PublicStatsOut` (Task 10), `PublicLayout`.
- Produces: `COMPONENTS: SystemComponent[]` where
  `SystemComponent = { name: string; what: string }`; `getPublicStats(): Promise<PublicStats | null>`.

- [ ] **Step 1: Write the component-view data and its test**

Create `frontend/src/guide/components.ts`:

```typescript
/**
 * The five components of canopy, and the one sentence that connects them.
 * This is the "component view" the public explainer is built around — the thing
 * that was actually missing from /system, which catalogues the plugin's
 * capabilities and never says what the system is made of.
 */
export interface SystemComponent {
  name: string
  what: string
}

export const COMPONENTS: SystemComponent[] = [
  {
    name: 'canopy-web',
    what: 'The system of record and every human surface. Owns workspaces, agents, turns, items, schedules, sessions and published artifacts.',
  },
  {
    name: 'The canopy plugin',
    what: 'The capability library — skills, agents and commands, installed into Claude Code. What an agent knows how to do.',
  },
  {
    name: 'Runners',
    what: 'The execution substrate. A paired laptop or a cloud box. Claims turns and runs Claude Code.',
  },
  {
    name: 'Agents',
    what: 'Persona repos stamped from a factory — identity, domain skills, hooks.',
  },
  {
    name: 'The harness',
    what: 'The control plane joining them: turn lifecycle, claim and lease, the event ledger, routing.',
  },
]

export const ONE_SENTENCE =
  'A turn is the unit of work: canopy-web owns the record of turns, runners execute them wherever your compute happens to live, and the plugin is the library of what an agent knows how to do.'
```

Create `frontend/src/guide/components.test.ts`:

```typescript
import { describe, it, expect } from 'vitest'
import { COMPONENTS, ONE_SENTENCE } from './components'

describe('COMPONENTS', () => {
  it('names five components with unique names', () => {
    expect(COMPONENTS).toHaveLength(5)
    expect(new Set(COMPONENTS.map((c) => c.name)).size).toBe(5)
  })

  it('explains each one', () => {
    for (const c of COMPONENTS) expect(c.what.length).toBeGreaterThan(20)
  })

  it('has a connecting sentence', () => {
    expect(ONE_SENTENCE).toContain('turn')
  })
})
```

- [ ] **Step 2: Run it and confirm it passes**

Run: `cd frontend && npx vitest run src/guide/components.test.ts`
Expected: 3 passed.

- [ ] **Step 3: Write the anonymous client call**

Create `frontend/src/api/publicStats.ts`:

```typescript
/**
 * Aggregate counts for the public explainer. Anonymous — deliberately a bare
 * fetch with no credentials and no CSRF, because this page is served to
 * visitors with no session and sending cookies would gain nothing.
 */
import { apiUrl } from './base'

export interface PublicStats {
  agents: number
  skills: number
  runners_online: number
  turns_executed: number
  demos_published: number
}

export async function getPublicStats(): Promise<PublicStats | null> {
  try {
    const resp = await fetch(apiUrl('/api/system/public-stats'))
    if (!resp.ok) return null
    return (await resp.json()) as PublicStats
  } catch {
    // The page must render without numbers rather than fail — a visitor who
    // cannot reach the API should still learn what canopy is.
    return null
  }
}
```

- [ ] **Step 4: Write the page**

Create `frontend/src/pages/AboutPage.tsx`:

```tsx
import { useEffect, useState } from 'react'
import { COMPONENTS, ONE_SENTENCE } from '@/guide/components'
import { USER_PATHS } from '@/guide/paths'
import { getPublicStats, type PublicStats } from '@/api/publicStats'

/**
 * The public explainer. Chrome-less, anonymous, mounted OUTSIDE AppLayout so
 * the shell's authed calls cannot bounce a visitor to login.
 *
 * Counts are live because prose goes stale within a month — "we run a fleet of
 * agents" reads as a claim, while "2 runners online" reads as a fact. The page
 * renders fine without them.
 */
export function AboutPage() {
  const [stats, setStats] = useState<PublicStats | null>(null)

  useEffect(() => {
    void getPublicStats().then(setStats)
  }, [])

  const figures: Array<[string, number | undefined]> = [
    ['agents', stats?.agents],
    ['skills', stats?.skills],
    ['runners online', stats?.runners_online],
    ['turns executed', stats?.turns_executed],
    ['demos published', stats?.demos_published],
  ]

  return (
    <div className="min-h-screen bg-background">
      <div className="mx-auto max-w-3xl px-6 py-16">
        <h1 className="text-2xl font-semibold text-foreground">Canopy</h1>
        <p className="mt-4 text-[15px] leading-relaxed text-foreground-secondary">
          Canopy runs a fleet of AI agents that do real work — shipping code, triaging
          mail, building demos — and keeps the durable record of what they did, so the
          work survives the session it happened in.
        </p>

        {stats ? (
          <dl className="mt-8 grid grid-cols-2 gap-4 sm:grid-cols-3">
            {figures.map(([label, value]) => (
              <div key={label} className="rounded-lg border border-border bg-card p-3">
                <dd className="text-xl font-semibold text-foreground">{value ?? '—'}</dd>
                <dt className="mt-0.5 text-[11px] uppercase tracking-wide text-muted-foreground">
                  {label}
                </dt>
              </div>
            ))}
          </dl>
        ) : null}

        <section className="mt-12">
          <h2 className="text-sm font-semibold text-foreground">How it fits together</h2>
          <p className="mt-2 text-[13px] leading-relaxed text-foreground-secondary">{ONE_SENTENCE}</p>
          <div className="mt-4 space-y-2">
            {COMPONENTS.map((c) => (
              <div key={c.name} className="rounded-lg border border-border bg-card p-3">
                <h3 className="text-[13px] font-semibold text-foreground">{c.name}</h3>
                <p className="mt-1 text-[13px] leading-relaxed text-foreground-secondary">{c.what}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="mt-12">
          <h2 className="text-sm font-semibold text-foreground">Five ways in</h2>
          <div className="mt-4 space-y-3">
            {USER_PATHS.map((p) => (
              <div key={p.id} className="rounded-lg border border-border bg-card p-4">
                <h3 className="text-[13px] font-semibold text-foreground">{p.title}</h3>
                <p className="mt-0.5 text-[12px] text-muted-foreground">{p.who}</p>
                <p className="mt-2 text-[13px] text-foreground-secondary">{p.startHere}</p>
                {p.note ? <p className="mt-1 text-[12px] text-foreground-subtle">{p.note}</p> : null}
              </div>
            ))}
          </div>
        </section>

        <footer className="mt-12 border-t border-border pt-6 text-[12px] text-muted-foreground">
          <a href="./api/docs/" className="text-primary hover:underline">
            API reference
          </a>
        </footer>
      </div>
    </div>
  )
}
```

- [ ] **Step 5: Mount the route on `PublicLayout`**

In `frontend/src/router.tsx`, add to the existing `PublicLayout` children array (beside
`/ddd-release/...`, `/storyboard/:slug`, `/narrative/:slug`):

```tsx
      { path: '/about', element: <AboutPage /> },
```

It must go in the `PublicLayout` block, **not** the `AppLayout` block — `AppLayout` fires
authed calls that bounce anonymous visitors to login.

- [ ] **Step 5b: Give `/about` a descriptor**

`/about` is a real route, so `isDocumentable('/about')` is true and Task 6's coverage test
will fail until it is described. It gets a descriptor rather than an entry in
`NOT_DOCUMENTABLE`, which stays reserved for redirects and the catch-all. Add to
`frontend/src/guide/surfaces.ts`:

```typescript
  {
    path: '/about',
    title: 'About Canopy (public)',
    audience: 'Anyone, including people with no account',
    what: 'The public explainer — what Canopy is, the five components it is made of, live counts, and the five ways in. No login required, so this is the link to send someone outside the team.',
    actions: ['Send it to someone', 'See the live fleet counts'],
  },
```

Run `npx vitest run src/guide/coverage.test.ts` and confirm it is green before moving on.

- [ ] **Step 6: Allowlist the page path in the login middleware**

In `apps/common/middleware.py`, add to `PUBLIC_PATH_PREFIXES`:

```python
    "/about",  # the public explainer page shell (its stats API is allowlisted above)
```

- [ ] **Step 7: Add it to the PWA navigate-fallback allowlist**

In `frontend/src/pwa/navigation-fallback.ts`, add `/about` to
`NAVIGATE_FALLBACK_ALLOWLIST` following the existing entries' regex style, and add a unit
test beside the existing ones asserting `/about` is handled and, say, `/api/whatever` is
not. The allowlist is fail-safe by design: a forgotten SPA route only loses *offline*
shell fallback, but adding it is one line and the file is already unit-tested.

- [ ] **Step 8: Run everything**

```bash
cd frontend && npm run test && npm run build
cd .. && uv run pytest -q
```

Expected: all green. The `paths.test.ts` coupling test still passes because `/about` is a
public page, not a referenced surface.

- [ ] **Step 9: Verify anonymously in a real browser**

Start the app. Open `/about` in a **private/incognito window** with no session. Confirm:
the page renders, the counts appear, and you are **not** redirected to Google. Then check
the dark and light themes both read correctly (`PublicLayout` wraps `ThemeProvider`), and
confirm no raw palette literals slipped in:

```bash
cd frontend && grep -nE "stone-|orange-|zinc-|slate-|red-[0-9]|amber-|emerald-|sky-|violet-" src/pages/AboutPage.tsx src/pages/GuidePage.tsx src/pages/FirstRunPage.tsx src/components/settings/TokensPanel.tsx
```

Expected: no matches.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/guide/components.ts frontend/src/guide/components.test.ts frontend/src/api/publicStats.ts frontend/src/pages/AboutPage.tsx frontend/src/guide/surfaces.ts frontend/src/router.tsx frontend/src/pwa/navigation-fallback.ts apps/common/middleware.py
git commit -m "web: public explainer at /about

Chrome-less on PublicLayout, anonymous, with live aggregate counts because
prose goes stale within a month. Its five-ways-in section renders the same
registry the in-app guide does, so the public promise cannot drift from the
internal documentation."
```

---

## Task 12: Open the PR

- [ ] **Step 1: Run the full suite one more time**

```bash
uv run pytest -q
cd frontend && npm run test && npm run build
```

Expected: all green. Do not proceed on a failure — report it instead.

- [ ] **Step 2: Confirm generated types are committed and fresh**

```bash
cd frontend && npm run gen:api && git diff --stat src/api/generated.ts
```

Expected: no diff. A diff means `generated.ts` is stale; commit the regenerated file.
`regen-openapi.yml` checks this but is **not** a required check, so a stale file can merge
past it.

- [ ] **Step 3: Push and open the PR with auto-merge armed**

```bash
git push -u origin HEAD
gh pr create --title "Opening canopy to other people: first run, /guide, /about" --body-file - <<'BODY'
Implements `docs/superpowers/specs/2026-09-12-opening-canopy-to-other-people-design.md`.

**A1 — first run works.** A signed-in user with no workspace membership previously
landed on a blank page (`router.tsx` returned `null`). They now get a real first-run
screen with three states — including the case that matters for security: an
invite-admitted user with no membership *cannot* create a workspace (the F1 finding), so
`/api/me/` now reports eligibility and the screen asks them for an invite instead of
offering a button that 403s. Plus "+ Workspace" always reachable from the header, and
PAT list/mint/revoke on `/settings`.

**B — the app documents itself.** A descriptor registry keyed by the route table, a
`/guide` page grouped by the existing nav, and one fast two-direction coverage test:
every documented route has a descriptor, and every descriptor names a real route. The
second direction is what catches a descriptor orphaned by a deleted route. Empty states
deep-link into the guide so the stuck-user surface and the docs are the same content.

**C — a public explainer.** `/about`, chrome-less and anonymous, with the component view
and live aggregate counts from a new `GET /api/system/public-stats` (integers only; the
test asserts the shape is closed by type rather than by a denylist of field names).

Deliberately **not** here: GitHub-backed agent creation, which is its own spec
(`2026-09-12-github-backed-agent-creation-design.md`) because it needs a GitHub App
registered before it can be tested. The agents empty state teaches
`/canopy:create-agent` for now and becomes a button later without a rewrite.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
```

- [ ] **Step 4: Arm auto-merge and verify it took**

```bash
gh pr merge <n> --auto
gh pr view <n> --json autoMergeRequest
```

Never pass `--squash` or any strategy flag: the merge queue owns the strategy, and `gh`
answers a strategy flag with a warning that leaves auto-merge UNARMED. If
`autoMergeRequest` is `null`, re-run `gh pr merge <n> --auto`.
