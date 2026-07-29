# Supervisor Runners First-Class Tab — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote runners to their own **Runners** tab on `/supervisor` that shows each runner's health/info plus which agents prioritize it (by runner kind), with the agent's kind-preference reorderable in place.

**Architecture:** Pure-frontend change. A new isolated derivation helper maps the already-loaded agents + runners into "which agents prioritize this runner's kind." A new 4th tab on `SupervisorPage` reuses the existing `RunnerStatus` list → `RunnerDetail` detail flow, with `RunnerDetail` gaining an "Agent priority" section that embeds the existing `RunnerOrder` editor per agent. No backend, API, schema, or migration work.

**Tech Stack:** React 19 + TypeScript + Vite + Tailwind 4 + `canopy-ui`. Tests: Vitest (node env — pure-function unit tests only; **no DOM/testing-library in this repo**). Type/component verification via `npm run build`.

## Global Constraints

- **Design tokens only — no raw palette literals.** Use `bg-card`, `border-border`, `text-foreground`, `text-muted-foreground`, `text-primary`, `bg-primary/15`, `bg-muted`, `text-foreground-subtle`, etc. Never `stone-*`/`orange-*`/`zinc-*`/etc. (CLAUDE.md).
- **`Agent.runner_preference` is an ordered list of runner KINDS** (`cloud` / `emdash` / `remote`), not runner instances. Empty/absent = "any eligible runner, first-poll-wins" (implicitly accepts every kind).
- **Phone-first single column.** The page is `max-w-2xl`; keep rows dense and compact.
- **Pure-function unit tests only.** This repo's vitest has no DOM environment — do NOT write React component/render tests. Verify components with `npm run build`.
- **Verify in the running app, not curl** — canopy-web is a PWA; stale service-worker bundles can mask new routes (memory: verify-frontend-render-not-curl).
- **Commit after each task.**

## File Structure

- `frontend/src/components/supervisor/runnerPriority.ts` — **new.** Pure derivation: `agentsForKind`, `firstChoiceCount`, `ordinal`.
- `frontend/src/components/supervisor/runnerPriority.test.ts` — **new.** Unit tests for the helper.
- `frontend/src/components/agents/RunnerOrder.tsx` — **modify.** Add optional `onSaved` + `runners` props (backward compatible).
- `frontend/src/components/supervisor/RunnerDetail.tsx` — **modify.** Add the "Agent priority" section + inline `RunnerOrder`.
- `frontend/src/components/supervisor/RunnerStatus.tsx` — **modify.** Add the "N agents" chip.
- `frontend/src/pages/SupervisorPage.tsx` — **modify.** 4th tab, move runner UI out of Agents, pass data down, lift preference updates.

Reference types (already generated, do not change):
- `AgentOut = Schemas['AgentOut']` with `runner_preference?: readonly string[] | null`, `slug: string`, `name: string` (`frontend/src/api/agents.ts:10`).
- `RunnerOut = components['schemas']['RunnerOut']` with `kind: string`, `status`, `last_heartbeat_at`, `host`, `ready`, `capabilities`, `code_branch`, `workspace` (`frontend/src/api/harness.ts:7`).
- `updateAgentRunnerPreference(slug, runnerPreference: string[])` (`frontend/src/api/agents.ts:194`).

---

### Task 1: Pure derivation helper `runnerPriority.ts`

**Files:**
- Create: `frontend/src/components/supervisor/runnerPriority.ts`
- Test: `frontend/src/components/supervisor/runnerPriority.test.ts`

**Interfaces:**
- Consumes: `AgentOut` from `@/api/agents`.
- Produces:
  - `interface RankedAgent { agent: AgentOut; rank: number }` (rank is 1-based)
  - `interface KindPriority { ranked: RankedAgent[]; acceptsAll: AgentOut[] }`
  - `agentsForKind(agents: AgentOut[], kind: string): KindPriority`
  - `firstChoiceCount(agents: AgentOut[], kind: string): number`
  - `ordinal(n: number): string`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/supervisor/runnerPriority.test.ts`:

```ts
import { describe, expect, it } from 'vitest'

import { agentsForKind, firstChoiceCount, ordinal } from './runnerPriority'
import type { AgentOut } from '@/api/agents'

// AgentOut has many fields; the helper only reads slug/name/runner_preference.
const agent = (slug: string, runner_preference: string[] | null): AgentOut =>
  ({ slug, name: slug, runner_preference } as unknown as AgentOut)

describe('agentsForKind', () => {
  it('ranks agents by the position of the kind in their preference', () => {
    const agents = [
      agent('a', ['cloud', 'emdash']), // cloud rank 1
      agent('b', ['emdash', 'cloud']), // cloud rank 2
    ]
    const { ranked } = agentsForKind(agents, 'cloud')
    expect(ranked.map((r) => [r.agent.slug, r.rank])).toEqual([
      ['a', 1],
      ['b', 2],
    ])
  })

  it('sorts ranked agents ascending by rank regardless of input order', () => {
    const agents = [
      agent('b', ['emdash', 'cloud']), // rank 2
      agent('a', ['cloud']), // rank 1
    ]
    const { ranked } = agentsForKind(agents, 'cloud')
    expect(ranked.map((r) => r.agent.slug)).toEqual(['a', 'b'])
  })

  it('treats an empty preference as accepts-all, not ranked', () => {
    const { ranked, acceptsAll } = agentsForKind([agent('a', [])], 'cloud')
    expect(ranked).toEqual([])
    expect(acceptsAll.map((x) => x.slug)).toEqual(['a'])
  })

  it('treats a null/absent preference as accepts-all', () => {
    const { acceptsAll } = agentsForKind([agent('a', null)], 'cloud')
    expect(acceptsAll.map((x) => x.slug)).toEqual(['a'])
  })

  it('excludes agents whose non-empty preference omits the kind', () => {
    const { ranked, acceptsAll } = agentsForKind([agent('a', ['emdash'])], 'cloud')
    expect(ranked).toEqual([])
    expect(acceptsAll).toEqual([])
  })
})

describe('firstChoiceCount', () => {
  it('counts only agents whose #1 kind matches', () => {
    const agents = [
      agent('a', ['cloud', 'emdash']),
      agent('b', ['cloud']),
      agent('c', ['emdash', 'cloud']), // cloud is 2nd — not counted
      agent('d', []), // no preference — not counted
    ]
    expect(firstChoiceCount(agents, 'cloud')).toBe(2)
  })
})

describe('ordinal', () => {
  it('formats common ordinals', () => {
    expect(ordinal(1)).toBe('1st')
    expect(ordinal(2)).toBe('2nd')
    expect(ordinal(3)).toBe('3rd')
    expect(ordinal(4)).toBe('4th')
  })
  it('handles the 11-13 teens exception', () => {
    expect(ordinal(11)).toBe('11th')
    expect(ordinal(12)).toBe('12th')
    expect(ordinal(13)).toBe('13th')
    expect(ordinal(21)).toBe('21st')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/supervisor/runnerPriority.test.ts`
Expected: FAIL — cannot resolve `./runnerPriority` (module not found).

- [ ] **Step 3: Write the implementation**

Create `frontend/src/components/supervisor/runnerPriority.ts`:

```ts
import type { AgentOut } from '@/api/agents'

export interface RankedAgent {
  agent: AgentOut
  /** 1-based position of the runner kind in the agent's runner_preference. */
  rank: number
}

export interface KindPriority {
  /** Agents whose preference includes the kind, sorted by rank ascending. */
  ranked: RankedAgent[]
  /** Agents with an empty/absent preference — implicitly accept every kind. */
  acceptsAll: AgentOut[]
}

// Which agents route work to a runner of `kind`, and how strongly.
// runner_preference is an ordered list of runner KINDS; empty/absent means
// "any eligible runner, first-poll-wins" (implicitly accepts every kind).
export function agentsForKind(agents: AgentOut[], kind: string): KindPriority {
  const ranked: RankedAgent[] = []
  const acceptsAll: AgentOut[] = []
  for (const agent of agents) {
    const pref = agent.runner_preference ?? []
    if (pref.length === 0) {
      acceptsAll.push(agent)
      continue
    }
    const idx = pref.indexOf(kind)
    if (idx >= 0) ranked.push({ agent, rank: idx + 1 })
    // A non-empty preference that omits `kind` never claims it — excluded.
  }
  ranked.sort((a, b) => a.rank - b.rank) // Array.sort is stable (ES2019+)
  return { ranked, acceptsAll }
}

// Count of agents that rank `kind` as their #1 choice — the runner's true
// prioritizers, shown as the list-view chip.
export function firstChoiceCount(agents: AgentOut[], kind: string): number {
  return agents.filter((a) => (a.runner_preference ?? [])[0] === kind).length
}

// 1 -> "1st", 2 -> "2nd", 3 -> "3rd", 11 -> "11th", 21 -> "21st".
export function ordinal(n: number): string {
  const rem100 = n % 100
  if (rem100 >= 11 && rem100 <= 13) return `${n}th`
  switch (n % 10) {
    case 1:
      return `${n}st`
    case 2:
      return `${n}nd`
    case 3:
      return `${n}rd`
    default:
      return `${n}th`
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/components/supervisor/runnerPriority.test.ts`
Expected: PASS (3 describe blocks, all cases green).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/supervisor/runnerPriority.ts frontend/src/components/supervisor/runnerPriority.test.ts
git commit -m "feat(supervisor): runner-priority derivation helper (agents-for-kind)"
```

---

### Task 2: `RunnerOrder` — add `onSaved` + `runners` reuse props

Make the existing per-agent runner-order editor embeddable on the supervisor: notify the parent after a save, and accept an already-loaded runner list to avoid a redundant `listRunners()` fetch per embedded instance. Both props are optional — the existing agent-overview caller is unaffected.

**Files:**
- Modify: `frontend/src/components/agents/RunnerOrder.tsx`

**Interfaces:**
- Consumes: `updateAgentRunnerPreference` (unchanged), `RunnerOut`.
- Produces: `RunnerOrder` now accepts optional `onSaved?: (pref: string[]) => void` and `runners?: RunnerOut[]`.

- [ ] **Step 1: Widen the props and rename the internal fetched-runners state**

In `frontend/src/components/agents/RunnerOrder.tsx`, replace the component signature + the runners state + the fetch effect (current lines 25–46) with:

```tsx
export function RunnerOrder({
  slug,
  name,
  preference,
  onSaved,
  runners: runnersProp,
}: {
  slug: string
  name: string
  preference: readonly string[]
  onSaved?: (pref: string[]) => void
  runners?: RunnerOut[]
}): JSX.Element {
  const [order, setOrder] = useState<string[]>([...preference])
  const [fetched, setFetched] = useState<RunnerOut[]>([])
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Only self-fetch when the caller didn't supply a runner list.
  useEffect(() => {
    if (runnersProp) return
    listRunners()
      .then(setFetched)
      .catch(() => setFetched([]))
  }, [runnersProp])

  const runners = runnersProp ?? fetched
  const online = useMemo(() => onlineByKind(runners), [runners])
```

(Leave the rest of the component — `dirty`, `unused`, `move`, `remove`, `add`, and the JSX — unchanged; they already reference `order`, `online`, and `runners` by those names.)

- [ ] **Step 2: Fire `onSaved` after a successful save**

In the same file, update `save` (current lines 71–82) so the success branch notifies the parent:

```tsx
  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      await updateAgentRunnerPreference(slug, order)
      setSaved(true)
      onSaved?.(order)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to save')
    } finally {
      setSaving(false)
    }
  }
```

- [ ] **Step 3: Typecheck the change**

Run: `cd frontend && npm run build`
Expected: build succeeds (no TS errors). The existing caller `AgentOverviewSection.tsx` passes neither new prop and still compiles.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/agents/RunnerOrder.tsx
git commit -m "feat(agents): RunnerOrder accepts onSaved + injected runners for reuse"
```

---

### Task 3: `RunnerDetail` — "Agent priority" section with inline edit

Add, below the existing runner info card, a section listing the agents that prioritize this runner's kind (ranked, with ordinals) plus an "accepts all" group, each expandable into an inline `RunnerOrder` editor.

**Files:**
- Modify: `frontend/src/components/supervisor/RunnerDetail.tsx`

**Interfaces:**
- Consumes: `agentsForKind`, `ordinal` (Task 1); `RunnerOrder` with `onSaved`/`runners` (Task 2); `AgentOut`, `RunnerOut`.
- Produces: `RunnerDetail` now requires `agents: AgentOut[]`, `runners: RunnerOut[]`, `onAgentSaved: (slug: string, pref: string[]) => void` in addition to the existing `runner` + `onBack`.

- [ ] **Step 1: Replace the file with the extended version**

Overwrite `frontend/src/components/supervisor/RunnerDetail.tsx` with:

```tsx
import { useState, type JSX } from 'react'
import type { RunnerOut } from '@/api/harness'
import type { AgentOut } from '@/api/agents'
import { RunnerOrder } from '@/components/agents/RunnerOrder'
import { agentsForKind, ordinal } from './runnerPriority'

// A runner's full state — the click-through from the Runners tab's runner list.
// Surfaces the two health signals that matter (ON / READY), the runner's info,
// and which agents prioritize this runner's KIND — editable in place.
export function RunnerDetail({
  runner,
  agents,
  runners,
  onAgentSaved,
  onBack,
}: {
  runner: RunnerOut
  agents: AgentOut[]
  runners: RunnerOut[]
  onAgentSaved: (slug: string, pref: string[]) => void
  onBack: () => void
}): JSX.Element {
  const online = runner.status === 'online'
  const caps = (runner.capabilities ?? {}) as { agents?: string[]; projects?: string[] }
  const { ranked, acceptsAll } = agentsForKind(agents, runner.kind)
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const toggle = (slug: string) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(slug)) next.delete(slug)
      else next.add(slug)
      return next
    })

  const row = (label: string, value: string) => (
    <div className="flex items-baseline justify-between gap-3 border-b border-border py-1.5">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="min-w-0 truncate text-[13px] text-foreground">{value}</span>
    </div>
  )

  const agentRow = (a: AgentOut, badge: string) => (
    <div key={a.slug} className="rounded-md border border-border bg-background">
      <button
        type="button"
        onClick={() => toggle(a.slug)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left"
        data-testid={`runner-priority-agent-${a.slug}`}
      >
        <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">{a.name}</span>
        <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">{badge}</span>
        <span className="shrink-0 text-muted-foreground">{expanded.has(a.slug) ? '▾' : '▸'}</span>
      </button>
      {expanded.has(a.slug) && (
        <div className="px-2 pb-2">
          <RunnerOrder
            slug={a.slug}
            name={a.name}
            preference={a.runner_preference ?? []}
            runners={runners}
            onSaved={(pref) => onAgentSaved(a.slug, pref)}
          />
        </div>
      )}
    </div>
  )

  return (
    <div className="flex flex-col gap-2" data-testid={`runner-detail-${runner.name}`}>
      <button type="button" onClick={onBack} className="self-start text-[12px] text-primary" data-testid="runner-detail-back">
        ← Runners
      </button>
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${online ? 'bg-success' : 'bg-muted-foreground'}`} />
        <span className="text-[15px] font-semibold text-foreground">{runner.name}</span>
        <span
          data-testid="runner-detail-ready"
          className={`ml-auto rounded px-1.5 py-0.5 text-[11px] ${runner.ready ? 'bg-success/15 text-success' : 'bg-destructive/15 text-destructive'}`}
        >
          {runner.ready ? 'ready' : 'not ready'}
        </span>
      </div>
      {!runner.ready && runner.ready_note && (
        <p className="text-[12px] text-destructive" data-testid="runner-detail-why">{runner.ready_note}</p>
      )}
      <div className="rounded-lg border border-border bg-card p-3">
        {row('status', online ? 'online' : (runner.status ?? 'unknown'))}
        {row('kind', runner.kind ?? '')}
        {row('host', runner.host ?? '')}
        {row('workspace', runner.workspace ?? '')}
        {row('agents', (caps.agents ?? []).join(', ') || '—')}
        {row('projects', (caps.projects ?? []).join(', ') || '—')}
      </div>

      {/* Which agents route work to this runner's KIND, and how strongly. */}
      <div className="flex flex-col gap-1.5" data-testid="runner-priority">
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
          Agent priority · {runner.kind || 'unknown'}
        </span>
        {ranked.length === 0 && acceptsAll.length === 0 && (
          <p className="text-[12px] text-muted-foreground">No agents prioritize this runner kind.</p>
        )}
        {ranked.map((r) => agentRow(r.agent, ordinal(r.rank)))}
        {acceptsAll.length > 0 && (
          <>
            <span className="mt-1 text-[11px] text-foreground-subtle">any — accepts all kinds</span>
            {acceptsAll.map((a) => agentRow(a, 'any'))}
          </>
        )}
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Typecheck**

Run: `cd frontend && npm run build`
Expected: FAIL at `SupervisorPage.tsx` — `RunnerDetail` is now missing the new required props there. That is fixed in Task 5. (If you are running tasks strictly independently, note this cross-task dependency; the build goes green after Task 5.)

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/supervisor/RunnerDetail.tsx
git commit -m "feat(supervisor): RunnerDetail shows agent priority + inline runner-order edit"
```

---

### Task 4: `RunnerStatus` — "N agents" chip

Show, on each runner row, how many agents rank that runner's kind #1.

**Files:**
- Modify: `frontend/src/components/supervisor/RunnerStatus.tsx`

**Interfaces:**
- Consumes: `firstChoiceCount` (Task 1); `AgentOut`.
- Produces: `RunnerStatus` accepts a new optional `agents?: AgentOut[]` prop (chip hidden when absent or count is 0).

- [ ] **Step 1: Add the imports**

At the top of `frontend/src/components/supervisor/RunnerStatus.tsx`, add below the existing imports:

```tsx
import type { AgentOut } from '@/api/agents'
import { firstChoiceCount } from './runnerPriority'
```

- [ ] **Step 2: Widen the props**

Replace the component signature (current lines 22–28) with:

```tsx
export function RunnerStatus({
  runners,
  agents,
  onSelect,
}: {
  runners: RunnerOut[]
  agents?: AgentOut[]
  onSelect?: (r: RunnerOut) => void
}): JSX.Element {
```

- [ ] **Step 3: Render the chip**

Replace the `runners.map(...)` body (current lines 38–56) with a version that computes and renders the chip before the heartbeat span:

```tsx
      {runners.map((r) => {
        const nAgents = agents ? firstChoiceCount(agents, r.kind) : 0
        return (
          <button
            key={r.id}
            type="button"
            onClick={() => onSelect?.(r)}
            className="flex w-full items-center gap-2.5 rounded-lg border border-border bg-card px-3 py-2 text-left"
            data-testid={`runner-${r.name}`}
          >
            <span className={`h-2 w-2 shrink-0 rounded-full ${DOT[r.status] ?? 'bg-muted-foreground'}`} />
            <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-foreground">{r.name}</span>
            {!r.ready && (
              <span data-testid={`runner-notready-${r.name}`} className="shrink-0 rounded bg-destructive/15 px-1 text-[10px] text-destructive">
                not ready
              </span>
            )}
            {r.host && <span className="hidden truncate text-[11px] text-foreground-subtle sm:inline">{r.host}</span>}
            {nAgents > 0 && (
              <span
                data-testid={`runner-agents-${r.name}`}
                className="shrink-0 rounded bg-primary/15 px-1.5 py-0.5 text-[10px] font-medium text-primary"
              >
                {nAgents} {nAgents === 1 ? 'agent' : 'agents'}
              </span>
            )}
            <span className="shrink-0 text-[11px] text-muted-foreground">{relative(r.last_heartbeat_at)}</span>
          </button>
        )
      })}
```

- [ ] **Step 4: Typecheck**

Run: `cd frontend && npm run build`
Expected: still FAIL only at `SupervisorPage.tsx` (RunnerDetail props from Task 3); `RunnerStatus.tsx` itself compiles. Goes green after Task 5.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/supervisor/RunnerStatus.tsx
git commit -m "feat(supervisor): runner rows show how many agents prioritize the kind"
```

---

### Task 5: `SupervisorPage` — 4th "Runners" tab + wiring

Add the Runners tab, move the runner UI out of the Agents tab, pass `agents`/`runners`/`onAgentSaved` into the runner components, and lift saved preferences into local `agents` state so the mapping + chip re-derive live.

**Files:**
- Modify: `frontend/src/pages/SupervisorPage.tsx`

**Interfaces:**
- Consumes: `RunnerStatus` (Task 4), `RunnerDetail` (Task 3).
- Produces: nothing downstream (page component).

- [ ] **Step 1: Add the preference-lift handler**

In `frontend/src/pages/SupervisorPage.tsx`, add this `useCallback` right after `reloadItems` (after current line 49). `useCallback` is already imported (line 1):

```tsx
  // After an inline runner-order save, patch the agent's preference in local
  // state so the Runners tab's priority list + "N agents" chip re-derive live.
  const handleAgentPreferenceSaved = useCallback((slug: string, pref: string[]) => {
    setAgents((prev) => prev?.map((a) => (a.slug === slug ? { ...a, runner_preference: pref } : a)) ?? prev)
  }, [])
```

- [ ] **Step 2: Widen the tab union**

Replace current line 115:

```tsx
  const tab = raw === 'sessions' || raw === 'agents' ? raw : 'inbox'
```

with:

```tsx
  const tab =
    raw === 'sessions' || raw === 'agents' || raw === 'runners' ? raw : 'inbox'
```

- [ ] **Step 3: Add the Runners tab trigger**

In the `<TabsList>` (current lines 156–171), add a fourth trigger after the Agents trigger (after current line 170):

```tsx
          <TabsTrigger value="runners" data-testid="tab-runners">
            Runners
          </TabsTrigger>
```

- [ ] **Step 4: Move the runner block into a new Runners TabsContent, and slim the Agents tab**

Replace the entire Agents `<TabsContent>` block (current lines 190–219) with the slimmed Agents tab **followed by** the new Runners tab:

```tsx
        {/* Agents — fleet KPIs + the one-time setup prompts. */}
        <TabsContent value="agents" className="flex flex-col gap-4">
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

        {/* Runners — fleet runner health + which agents prioritize each kind. */}
        <TabsContent value="runners" className="flex flex-col gap-4">
          {selectedRunner ? (
            <RunnerDetail
              runner={selectedRunner}
              agents={agents ?? []}
              runners={renderRunners ?? []}
              onAgentSaved={handleAgentPreferenceSaved}
              onBack={() => setSelectedRunner(null)}
            />
          ) : errs.runners ? (
            <BandError message={errs.runners} />
          ) : renderRunners === null ? (
            <Skeleton className="h-12 w-full" />
          ) : (
            <RunnerStatus runners={renderRunners} agents={agents ?? []} onSelect={setSelectedRunner} />
          )}
        </TabsContent>
```

- [ ] **Step 5: Full typecheck + build**

Run: `cd frontend && npm run build`
Expected: PASS — no TS errors. (The RunnerDetail/RunnerStatus prop errors from Tasks 3–4 are now resolved.)

- [ ] **Step 6: Run the unit suite**

Run: `cd frontend && npm run test`
Expected: PASS, including `runnerPriority.test.ts` and the pre-existing `useLiveSupervisor.test.ts`.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/SupervisorPage.tsx
git commit -m "feat(supervisor): Runners first-class tab (runner health + agent priority)"
```

---

### Task 6: Verify in the running app

Component behavior is not unit-testable here (no DOM env), so confirm it renders and round-trips in the real app.

**Files:** none (verification only).

- [ ] **Step 1: Start the app**

Run (from repo root): `uv run honcho start -f Procfile.dev` (or run backend + `cd frontend && npm run dev` separately). Ensure at least one runner is paired and some agents exist (seeded fleet).

- [ ] **Step 2: Verify the tab and mapping**

Open `http://localhost:3000/supervisor?tab=runners` in a browser (hard-reload / bypass the service worker so a stale bundle can't mask the new route — memory: verify-frontend-render-not-curl). Confirm:
  - A **Runners** tab appears (Inbox / Sessions / Agents / Runners) and is reachable via `?tab=runners`.
  - The runner list shows rows; a runner whose kind is some agent's #1 shows a "**N agents**" chip.
  - The **Agents** tab no longer shows the runner list — only KPI cards + install/push.

- [ ] **Step 3: Verify detail + inline edit round-trip**

Click a runner → the detail shows the info card **and** an "Agent priority · <kind>" section with ranked agents (1st/2nd/…) and any "accepts all" group. Expand an agent, reorder its kinds, Save → "Saved." appears; go back → the "N agents" chip and priority ordering reflect the change without a page reload.

- [ ] **Step 4: No commit** (verification only). If any defect is found, fix it in the owning task's file and amend/extend with a new commit.

---

## Self-Review Notes

- **Spec coverage:** §1 tab bar → Task 5; §2 list chip → Task 4; §3 detail + inline edit → Task 3; §4 helper → Task 1; §5 RunnerOrder tweaks → Task 2; testing → Tasks 1 (unit) + 5/6 (build + render). All spec sections mapped.
- **Type consistency:** `agentsForKind`/`firstChoiceCount`/`ordinal` signatures identical across Tasks 1, 3, 4. `onAgentSaved(slug, pref)` (RunnerDetail) ↔ `onSaved(pref)` (RunnerOrder) wired via `(pref) => onAgentSaved(a.slug, pref)`. `runner_preference` typed `readonly string[] | null` everywhere; helper reads `?? []`.
- **Cross-task build note:** Tasks 3 and 4 leave `SupervisorPage.tsx` temporarily failing typecheck (new required RunnerDetail props); Task 5 resolves it. Called out in each affected step so a task-by-task runner isn't surprised.
