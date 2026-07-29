# /supervisor tabs (Inbox · Sessions · Agents) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the flat five-section `/supervisor` scroll into three focused tabs — Inbox (default) / Sessions / Agents — so each glance shows one thing well.

**Architecture:** Pure recomposition of `SupervisorPage.tsx` on `canopy-ui`'s existing `Tabs` primitive. All data logic (the `allSettled` mount fetch, the `useLiveSupervisor` WebSocket overlay, `waitingFor`, `totalWaiting`, `setBadge`) is preserved verbatim — only the returned JSX is reorganized into tabs. The active tab syncs to a `?tab=` query param via `useSearchParams`. The section components (`WaitingOnYou`, `Composer`, `OpenSessions`, `AgentKpiCard`, `RunnerStatus`) are untouched.

**Tech Stack:** React 19 + Vite + Tailwind 4 + `canopy-ui` (Tabs on `@base-ui/react`) + `react-router-dom`; Playwright.

**Spec:** `docs/superpowers/specs/2026-07-17-supervisor-tabs-design.md`.

## Global Constraints

- **Design tokens only** — no raw Tailwind palette literals. Use `bg-card`, `border-border`, `text-foreground`, `text-muted-foreground`, `text-primary`, `bg-primary`, `bg-primary/15`, status tokens.
- **Reuse `canopy-ui`'s `Tabs`** (`Tabs`, `TabsList`, `TabsTrigger`, `TabsContent`, imported from `canopy-ui`) — do NOT hand-roll a tab widget.
- **Section components are unchanged** — this is a layout change confined to `SupervisorPage.tsx` and its e2e test. Do not modify `WaitingOnYou`/`Composer`/`OpenSessions`/`AgentKpiCard`/`RunnerStatus`.
- **Preserve ALL existing data logic verbatim** — the `useState` bands, the `allSettled` effect, `useLiveSupervisor`/`liveById`/`renderRunners`/`waitingFor`/`liveTotalWaiting`/`totalWaiting`, and the `setBadge` effect. Only the `return (…)` JSX changes.
- **Inbox is the default** — `/supervisor` with no `?tab=` (and any unknown value) resolves to Inbox; this is what push targets.
- **Only the Inbox tab carries a count badge** (`totalWaiting`, shown when > 0). Sessions/Agents have no badge.
- **No page-level horizontal scroll at phone width** on any tab.
- **CI does not run Playwright** — run `npx playwright test` locally before merge.
- **Never hand-edit `frontend/src/api/generated.ts`** — not touched by this change.

## File Structure

- `frontend/src/pages/SupervisorPage.tsx` — restructure the render into tabs (modify; data logic unchanged).
- `frontend/e2e/supervisor.spec.ts` — update existing tests to reach content via its tab; add default-tab / deep-link / per-tab-no-scroll tests (modify).

---

## Task 1: Restructure `/supervisor` into Inbox · Sessions · Agents tabs

**Files:**
- Modify: `frontend/src/pages/SupervisorPage.tsx`
- Modify: `frontend/e2e/supervisor.spec.ts`

**Interfaces:**
- Consumes: `canopy-ui`'s `Tabs, TabsList, TabsTrigger, TabsContent`; `react-router-dom`'s `useSearchParams`; the page's existing state + `useLiveSupervisor` derived values (unchanged).
- Produces: a three-tab `/supervisor`. Tab testids: `tab-inbox`, `tab-sessions`, `tab-agents`. Content testids are the components' existing ones (`waiting-on-you`/`waiting-empty`, `composer`, `open-sessions`/`sessions-empty`, `runner-status`/"No runner paired", the agent KPI grid).

- [ ] **Step 1: Update the Playwright tests for the tabbed layout (TDD — these fail against the current flat page)**

Open `frontend/e2e/supervisor.spec.ts`. Add a helper near the top of the file (after the imports, before the `test.describe`):

```typescript
// Reach a tab's content. Inbox is the default landing; Sessions/Agents need a click.
async function openTab(page: import('@playwright/test').Page, tab: 'inbox' | 'sessions' | 'agents') {
  if (tab !== 'inbox') await page.getByTestId(`tab-${tab}`).click()
}
```

Then make these changes inside the `test.describe('/supervisor', …)`:

**(a) Replace** the `renders the fleet at phone width without horizontal scroll` test body with a per-tab no-scroll check (runners moved off the default view, so the old runner-status assertion here is wrong):

```typescript
  test('renders without horizontal scroll on every tab', async ({ page }) => {
    await page.goto('/supervisor')
    await expect(page.getByTestId('supervisor-page')).toBeVisible()
    for (const tab of ['inbox', 'sessions', 'agents'] as const) {
      await openTab(page, tab)
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      )
      expect(overflow, `tab ${tab} overflows`).toBeLessThanOrEqual(0)
    }
  })
```

**(b) Add** a default-tab + deep-link test:

```typescript
  test('defaults to Inbox and deep-links via ?tab=', async ({ page }) => {
    // Default landing (what push drops you into) is Inbox: the waiting queue is
    // visible and the other tabs' content is not.
    await page.goto('/supervisor')
    await expect(page.getByTestId('waiting-on-you').or(page.getByTestId('waiting-empty'))).toBeVisible()
    await expect(page.getByTestId('open-sessions').or(page.getByTestId('sessions-empty'))).toBeHidden()

    // Deep-link straight to Agents.
    await page.goto('/supervisor?tab=agents')
    await expect(page.getByTestId('runner-status').or(page.getByText('No runner paired'))).toBeVisible()
  })
```

**(c)** For each existing test that touches the **composer** (`the composer dispatches a launchable command`, `a repo dispatch pins its workspace and routes to the tenant endpoint`) and the **open sessions** (`open sessions list and continue dispatches into that exact task`): insert `await openTab(page, 'sessions')` immediately after the `await page.goto('/supervisor')` line. The rest of each test is unchanged (the component testids are the same).

**(d) Replace** the `one failed call does not blank the page` test body — the aborted `needs-you` now surfaces on the Inbox band, and runners live on the Agents tab:

```typescript
  test('one failed call does not blank the page', async ({ page }) => {
    await page.route('**/api/agents/needs-you', (r) => r.abort())
    await page.goto('/supervisor')
    await expect(page.getByTestId('supervisor-page')).toBeVisible()
    // Runners (Agents tab) still render despite the Inbox fetch failing.
    await openTab(page, 'agents')
    await expect(page.getByTestId('runner-status').or(page.getByText('No runner paired'))).toBeVisible()
  })
```

**(e)** Leave `waiting-on-you is above the fold` as-is (Inbox is the default, so its content is present on load).

- [ ] **Step 2: Run the tests to verify they fail against the current flat page**

Run: `cd frontend && npx playwright test -g "supervisor" 2>&1 | tail -20; cd ..`
Expected: FAIL — the new `tab-inbox`/`tab-sessions`/`tab-agents` testids don't exist yet, and `open-sessions` is visible on load (flat page) so the "defaults to Inbox" hidden-assertion fails.

- [ ] **Step 3: Restructure `SupervisorPage.tsx`**

Change ONLY the imports and the `return (…)`. Keep every line from `const [agents, setAgents] …` through the `setBadge` effect exactly as it is.

Update the imports block — add the tabs + router hook, and note `InstallPrompt`/`PushToggle` stay imported (they move into the Agents tab, not removed):

```tsx
import { useEffect, useMemo, useState, type JSX } from 'react'
import { useSearchParams } from 'react-router-dom'
import { listAgents, getFleetNeedsYou, type AgentOut, type FleetNeedsYouOut } from '@/api/agents'
import { listRunners, type RunnerOut } from '@/api/harness'
import { useLiveSupervisor } from '@/hooks/useLiveSupervisor'
import { RunnerStatus } from '@/components/supervisor/RunnerStatus'
import { AgentKpiCard } from '@/components/supervisor/AgentKpiCard'
import { WaitingOnYou } from '@/components/supervisor/WaitingOnYou'
import { Composer } from '@/components/supervisor/Composer'
import { OpenSessions } from '@/components/supervisor/OpenSessions'
import { InstallPrompt } from '@/pwa/InstallPrompt'
import { PushToggle } from '@/pwa/PushToggle'
import { setBadge } from '@/pwa/usePush'
import { Skeleton, Tabs, TabsList, TabsTrigger, TabsContent } from 'canopy-ui'
```

Replace the entire `return (…)` with:

```tsx
  const [searchParams, setSearchParams] = useSearchParams()
  const raw = searchParams.get('tab')
  // Unknown / absent value falls back to Inbox — never a blank tab, and no param
  // means Inbox (what push targets).
  const tab = raw === 'sessions' || raw === 'agents' ? raw : 'inbox'
  const onTab = (value: string) =>
    // Push history (not replace) so the phone back button steps through tabs.
    // Inbox is the bare URL; the others carry ?tab=.
    setSearchParams(value === 'inbox' ? {} : { tab: value })

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4 p-4" data-testid="supervisor-page">
      <header>
        <h1 className="text-lg font-semibold text-foreground">Supervisor</h1>
        <p className="mt-0.5 text-[12px] text-muted-foreground">Your fleet, and what it needs from you.</p>
      </header>

      <Tabs value={tab} onValueChange={onTab} className="gap-4">
        <TabsList className="w-full">
          <TabsTrigger value="inbox" data-testid="tab-inbox">
            Inbox
            {totalWaiting > 0 && (
              <span className="ml-1 rounded bg-primary/15 px-1.5 py-0.5 text-[11px] font-medium text-primary">
                {totalWaiting}
              </span>
            )}
          </TabsTrigger>
          <TabsTrigger value="sessions" data-testid="tab-sessions">
            Sessions
          </TabsTrigger>
          <TabsTrigger value="agents" data-testid="tab-agents">
            Agents
          </TabsTrigger>
        </TabsList>

        {/* Inbox — the fleet's "waiting on you" queue (the act-now surface). */}
        <TabsContent value="inbox" className="flex flex-col gap-3">
          {errs.fleet ? (
            <BandError message={errs.fleet} />
          ) : fleet === null ? (
            <Skeleton className="h-24 w-full" />
          ) : (
            <WaitingOnYou fleet={fleet} />
          )}
        </TabsContent>

        {/* Sessions — dispatch, then the open emdash sessions you can continue. */}
        <TabsContent value="sessions" className="flex flex-col gap-4">
          {agents && agents.length > 0 && <Composer agents={agents} />}
          <OpenSessions />
        </TabsContent>

        {/* Agents — fleet KPIs + runner status + the one-time setup prompts. */}
        <TabsContent value="agents" className="flex flex-col gap-4">
          {errs.runners ? (
            <BandError message={errs.runners} />
          ) : renderRunners === null ? (
            <Skeleton className="h-12 w-full" />
          ) : (
            <RunnerStatus runners={renderRunners} />
          )}

          {errs.agents ? (
            <BandError message={errs.agents} />
          ) : agents === null ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {agents.map((a) => (
                <AgentKpiCard key={a.slug} agent={a} waiting={waitingFor(a.slug)} />
              ))}
            </div>
          )}

          <InstallPrompt />
          <PushToggle />
        </TabsContent>
      </Tabs>
    </div>
  )
```

(The `BandError` helper defined at the top of the file is unchanged and still used.)

- [ ] **Step 4: Typecheck + build**

Run: `cd frontend && npm run build 2>&1 | grep -E "error|✓ built"; cd ..`
Expected: `✓ built`, no type errors. (If `onValueChange`'s param type complains, it is `(value: string) => void` per base-ui — keep the `value: string` annotation.)

- [ ] **Step 5: Run the Playwright suite to verify green**

Run: `cd frontend && npx playwright test -g "supervisor" 2>&1 | tail -6; cd ..`
Expected: PASS on desktop + Pixel 7 — the default-Inbox, deep-link, per-tab-no-scroll, composer-under-Sessions, and open-sessions-under-Sessions tests all pass.

- [ ] **Step 6: Run vitest (nothing new expected, guard against a regression)**

Run: `cd frontend && npm run test 2>&1 | grep -E "Tests "; cd ..`
Expected: PASS (unchanged count — no new pure helper was extracted; the tab resolution is a one-line inline lookup).

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/SupervisorPage.tsx frontend/e2e/supervisor.spec.ts
git commit -m "feat(supervisor): three tabs — Inbox · Sessions · Agents"
```

---

## Final verification (before PR)

- [ ] `cd frontend && npm run build && npm run test && npx playwright test` — all green (full Playwright run, not just `-g supervisor`, to confirm no other spec depended on the old flat layout).
- [ ] Manually confirm at phone width: Inbox is the landing, the tab bar is thumb-reachable, switching tabs is instant, and the browser back button steps between tabs.
- [ ] PR, CI green (Backend + Frontend build; no regen — no schema change), merge, deploy, verify the live image tag == the merge SHA. Frontend-only, no migration.

## Self-review notes (coverage against the spec)

- Three tabs Inbox/Sessions/Agents on `canopy-ui` `Tabs` → Step 3. Inbox default + `?tab=` sync + unknown-value fallback → the `tab`/`onTab` block. Only Inbox badge (`totalWaiting`) → the `TabsTrigger value="inbox"`. Setup prompts into Agents → the Agents `TabsContent`. Data loading + live overlay unchanged → everything above the `return` is preserved verbatim. Per-tab no-scroll + default + deep-link tested → Step 1.
- Not changed: any section component, any API, the push trigger. No migration.
