import { useCallback, useEffect, useState, type JSX } from 'react'
import { Link, Navigate, Outlet, useNavigate, useOutletContext, useParams } from 'react-router-dom'
import { listMyAgents, type AgentOut } from '@/api/agents'
import { getRunner, type RunnerOut } from '@/api/harness'
import { ObjectPage } from '@/components/objectPage/ObjectPage'
import type { ObjectTab } from '@/components/objectPage/ObjectTabs'
import { scopedPath } from '@/lib/scopedLinks'
import { RunnerOverview, runnerBadge } from './RunnerOverview'
import { RunnerAccessTab, RunnerCredentialsTab, RunnerRoutingTab, RunnerSettingsTab } from './RunnerTabs'

// A RUNNER'S ONE PAGE, `/w/:workspace/runners/:runnerId` — every control for one
// box, behind tabs, at one address that alerts, mail, the supervisor, the runner
// tables and the fleet map all link to (docs/architecture/pages.md § One editor
// per object). It was a single column in the supervisor: facts, health, eight
// settings, credentials, admins, workspaces, readiness checks, retire and the
// routing matrix, one under the other.
//
//   Overview     — is it working: status, what it runs, alerts, health, readiness
//   Credentials  — its Claude logins, tokens and API keys, and the vault they live in
//   Routing      — which workspaces it serves, which agents it runs, who routes here
//   Access       — who else administers it, private to its owner
//   Settings     — pause, sessions, what it declares, egress, runtime, retire

export interface RunnerOutletContext {
  runner: RunnerOut
  /** Every agent the viewer can see — the routing matrix's rows, and where each
   *  lives (a health check links to its fix). Null until loaded. */
  agents: AgentOut[] | null
  /** A write changed the runner server-side: the page shows the fresh row. */
  onChanged: (runner: RunnerOut) => void
  /** The runner was retired: the page has nothing left to show. */
  onRetired: () => void
}

export type RunnerTab = 'overview' | 'credentials' | 'routing' | 'access' | 'settings'

/** The tabs this viewer gets: Credentials only for a cloud box they administer
 *  (the routes behind it 404 for anyone else), the rest for everyone — each says
 *  what the viewer may change. */
export function runnerTabs(runner: RunnerOut, needsCredential = false): ObjectTab[] {
  const tabs: ObjectTab[] = [{ to: 'overview', label: 'Overview' }]
  if (runner.can_administer && runner.kind === 'cloud') {
    tabs.push({ to: 'credentials', label: 'Credentials', dot: needsCredential, dotLabel: 'a credential needs attention' })
  }
  tabs.push({ to: 'routing', label: 'Routing' }, { to: 'access', label: 'Access' }, { to: 'settings', label: 'Settings' })
  return tabs
}

/** A failing `claude.probe.*` check: a credential someone has to sign in again. */
function credentialFailing(runner: RunnerOut): boolean {
  return Object.values(runner.health_checks ?? {}).some(
    (c) => c?.name?.startsWith('claude.probe.') && c.status === 'fail',
  )
}

export function RunnerPage(): JSX.Element {
  const { workspace = '', runnerId = '' } = useParams()
  const navigate = useNavigate()
  const [runner, setRunner] = useState<RunnerOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [agents, setAgents] = useState<AgentOut[] | null>(null)
  useEffect(() => {
    listMyAgents({ limit: 100 }).then((p) => setAgents(p.items)).catch(() => setAgents([]))
  }, [])

  const load = useCallback(() => {
    setError(null)
    getRunner(runnerId)
      .then(setRunner)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not load this runner'))
  }, [runnerId])
  useEffect(() => load(), [load])

  if (error) {
    return (
      <div className="max-w-4xl">
        <Link to={scopedPath(workspace, '/settings/topology/runners')} className="text-[12px] text-primary hover:underline">
          ← Runners
        </Link>
        <p className="mt-6 text-sm text-destructive" data-testid="runner-page-error">{error}</p>
      </div>
    )
  }
  if (!runner) return <div className="h-8 w-64 animate-pulse rounded-md bg-muted" data-testid="runner-page-loading" />

  // The address names the runner's workspace; a link with another one still opens
  // it, at its own address.
  if (runner.workspace && runner.workspace !== workspace) {
    return <Navigate to={scopedPath(runner.workspace, `/runners/${runner.id}`)} replace />
  }

  const badge = runnerBadge(runner)
  const ctx: RunnerOutletContext = {
    runner,
    agents,
    onChanged: setRunner,
    onRetired: () => navigate(scopedPath(workspace, '/settings/topology/runners')),
  }
  return (
    <ObjectPage
      title={<span className="font-mono">{runner.name}</span>}
      context="Runner"
      status={<span className={`rounded px-1.5 py-0.5 text-[11px] ${badge.cls}`} data-testid="runner-page-status">{badge.text}</span>}
      subtitle={`A runner on ${runner.machine ? `the machine ${runner.machine}` : runner.kind === 'cloud' ? 'a cloud machine' : 'a computer'}, owned by ${runner.owner_email ?? 'no one'}, in ${runner.workspace ?? 'no workspace'}.`}
      actions={<>
        <Link to={`/supervisor?tab=runners`} className="text-primary hover:underline">Live status →</Link>
        <Link to={scopedPath(workspace, `/settings/topology?runner=${runner.id}`)} className="text-primary hover:underline">
          On the fleet map →
        </Link>
      </>}
      tabsLabel="Runner"
      tabs={runnerTabs(runner, credentialFailing(runner))}
    >
      <Outlet context={ctx} />
    </ObjectPage>
  )
}

const TAB_COMPONENTS: Record<RunnerTab, (p: RunnerOutletContext) => JSX.Element | null> = {
  overview: RunnerOverview,
  credentials: RunnerCredentialsTab,
  routing: RunnerRoutingTab,
  access: RunnerAccessTab,
  settings: RunnerSettingsTab,
}

/** Route element for `:tab`: that tab's component, handed the page's runner. A tab
 *  this viewer does not get (or none by that name) opens Overview. */
export function RunnerTabRoute(): JSX.Element | null {
  const ctx = useOutletContext<RunnerOutletContext>()
  const { tab = '' } = useParams()
  const offered = runnerTabs(ctx.runner).some((t) => t.to === tab)
  const Tab = offered ? TAB_COMPONENTS[tab as RunnerTab] : undefined
  if (!Tab) return <Navigate to="../overview" relative="path" replace />
  return <Tab {...ctx} />
}
