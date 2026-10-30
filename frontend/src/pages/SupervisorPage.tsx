import { useCallback, useEffect, useMemo, useState, type JSX } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { listMyAgents, type AgentOut, type TaskOut } from '@/api/agents'
import {
  getFleetHold,
  listRunners,
  listUnclaimableTurns,
  type FleetHoldOut,
  type RunnerOut,
  type UnclaimableTurn,
} from '@/api/harness'
import { Menu } from 'lucide-react'
import {
  Button,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from 'canopy-ui/ui'
import { FleetHoldCard } from '@/components/supervisor/FleetHoldCard'
import { OperatorsCard } from '@/components/supervisor/OperatorsCard'
import { WorkspaceControlsCard } from '@/components/supervisor/WorkspaceControlsCard'
import { SessionFeed } from '@/components/supervisor/SessionFeed'
import { SignInRequests } from '@/components/supervisor/SignInRequests'
import { ActAsRequests } from '@/components/supervisor/ActAsRequests'
import { useLiveSupervisor } from '@/hooks/useLiveSupervisor'
import { usePolled } from '@/hooks/usePolled'
import { RunnerStatus } from '@/components/supervisor/RunnerStatus'
import { QueuedTurns } from '@/components/supervisor/QueuedTurns'
import { AlertList } from '@/components/alerts/AlertList'
import { loadAlerts } from '@/api/alerts'
import { AgentKpiCard } from '@/components/supervisor/AgentKpiCard'
import { WaitingOnYou, loadWaitingOnYou } from '@/components/supervisor/WaitingOnYou'
import { ChatSessionsPanel } from '@/components/chat/ChatSessionsPanel'
import { InstallPrompt } from '@/pwa/InstallPrompt'
import { PushToggle } from '@/pwa/PushToggle'
import { setBadge } from '@/pwa/usePush'
import { Skeleton } from 'canopy-ui'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { roleAllows } from '@/lib/workspaceRoles'
import { runnerHref } from '../lib/scopedLinks'

function BandError({ message }: { message: string }): JSX.Element {
  return (
    <p className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-[13px] text-destructive">
      {message}
    </p>
  )
}

// The supervisor's screens. The feed is home; the rest live behind the menu.
const SCREENS = [
  { id: 'feed', label: 'Feed' },
  { id: 'waiting', label: 'Waiting on you' },
  { id: 'sessions', label: 'Sessions' },
  { id: 'agents', label: 'Agents' },
  { id: 'runners', label: 'Runners' },
] as const
type Screen = (typeof SCREENS)[number]['id']

// The ONE supervisor surface (spec 2026-07-14). Three consumers will load this
// same route: the phone as an installed PWA, the menubar's WKWebView (Phase 5),
// and a desktop browser. Phone-first layout — a single column that widens.
//
// Home is a FEED of sessions that finished a turn and need your next prompt,
// readable and answerable in place (Jonathan, 2026-10-03). Waiting on you, Sessions,
// Agents and Runners are separate screens reached from the header menu.
export default function SupervisorPage(): JSX.Element {
  const [agents, setAgents] = useState<AgentOut[] | null>(null)
  const [runners, setRunners] = useState<RunnerOut[] | null>(null)
  const [waiting, setWaiting] = useState<TaskOut[] | null>(null)
  // Per-band errors, not one page-level error: on cellular a single flaky call
  // is the common case, and Promise.all would blank all three bands for it.
  const [errs, setErrs] = useState<{ agents?: string; runners?: string; waiting?: string }>({})

  // Reloadable on its own so acting on a task (approve/decline/reply) refreshes
  // Waiting on you without refetching agents + runners.
  const reloadWaiting = useCallback(() => {
    loadWaitingOnYou()
      .then((rows) => {
        setWaiting(rows)
        setErrs((e) => ({ ...e, waiting: undefined }))
      })
      .catch((err: unknown) =>
        setErrs((e) => ({ ...e, waiting: err instanceof Error ? err.message : 'Failed to load' })),
      )
  }, [])

  useEffect(() => {
    let cancelled = false
    const msg = (r: PromiseRejectedResult) =>
      r.reason instanceof Error ? r.reason.message : 'Failed to load'

    Promise.allSettled([listMyAgents({ limit: 100 }), listRunners(), loadWaitingOnYou()]).then(
      ([a, r, f]) => {
        if (cancelled) return
        if (a.status === 'fulfilled') setAgents(a.value.items)
        else setErrs((e) => ({ ...e, agents: msg(a) }))
        if (r.status === 'fulfilled') setRunners(r.value)
        else setErrs((e) => ({ ...e, runners: msg(r) }))
        if (f.status === 'fulfilled') setWaiting(f.value)
        else setErrs((e) => ({ ...e, waiting: msg(f) }))
      },
    )
    return () => {
      cancelled = true
    }
  }, [])

  // Queued turns nothing online can claim — polled with the runners, since
  // declaring a repo on a runner is what clears them. A fact list, not an alert:
  // the agent turns that need a routing fix are the `unclaimable_turns` alert.
  // A failed poll keeps the last list.
  const stuckPoll = usePolled(() => listUnclaimableTurns(), { ms: 30_000 })
  const stuck: UnclaimableTurn[] = stuckPoll.data ?? []
  const setStuck = stuckPoll.setData

  // The fleet hold — polled with the stuck list, since releasing it is what
  // unblocks the queue. Read by everyone; only a superuser gets the control.
  // A failed poll keeps the last value: the claim gate is server-side.
  const { data: fleetHoldData, setData: setFleetHold } = usePolled(() => getFleetHold(), { ms: 30_000 })
  const fleetHold: FleetHoldOut | null = fleetHoldData ?? null

  // Open alerts you can act on (apps/attention), for the badge and the feed's
  // one line. The Runners screen lists them with <AlertList>, which polls itself.
  const { data: openAlerts } = usePolled(() => loadAlerts(undefined, 'open'), { ms: 30_000 })

  // Re-poll runners so the table's facts (code, branch, readiness) follow without
  // a reload — they ride the REST runner, not the live socket overlay.
  // A failed re-poll keeps last-good; the mount fetch owns first-error surfacing.
  usePolled(() => listRunners().then(setRunners).catch(() => {}), { ms: 30_000, immediate: false })

  // Live overlay: snapshot + runner/waiting deltas over WS. Falls back silently
  // to the mount fetch above until the socket delivers a snapshot.
  const live = useLiveSupervisor()
  const liveById = useMemo(
    () => Object.fromEntries(live.runners.map((r) => [r.id, r] as const)),
    [live.runners],
  )
  // Runner rows with live status/heartbeat patched in when the socket knows them.
  const renderRunners: RunnerOut[] | null =
    runners?.map((r) => {
      const lr = liveById[r.id]
      return lr ? { ...r, status: lr.status, last_heartbeat_at: lr.last_heartbeat_at } : r
    }) ?? null
  // Waiting count per agent + total: prefer the live value once a snapshot lands,
  // else derive from the fetched waiting tasks.
  const waitingCountFor = (slug: string): number =>
    (waiting ?? []).filter((t) => t.agent_slug === slug).length
  const waitingFor = (slug: string): number =>
    live.hasSnapshot && slug in live.waiting ? live.waiting[slug] : waitingCountFor(slug)
  const liveTotalWaiting = Object.values(live.waiting).reduce((a, b) => a + b, 0)
  const totalWaiting = live.hasSnapshot ? liveTotalWaiting : (waiting?.length ?? 0)

  // The app-icon count. Android honours this; elsewhere it no-ops.
  useEffect(() => {
    if (live.hasSnapshot || waiting) setBadge(totalWaiting)
  }, [live.hasSnapshot, waiting, totalWaiting])

  const [searchParams, setSearchParams] = useSearchParams()
  const raw = searchParams.get('tab')
  // The bare URL is the FEED — sessions waiting for your next prompt. Every
  // other view is its own screen behind the menu, addressed by ?tab= (kept as
  // the param name so existing deep links — Settings → Runners, the topology
  // map — still land). The retired `?tab=inbox` lands on Waiting on you, its
  // successor, so old bookmarks still open the queue. Unknown values fall back
  // to the feed.
  const wanted = raw === 'inbox' ? 'waiting' : raw
  const tab: Screen = SCREENS.some((s) => s.id === wanted) ? (wanted as Screen) : 'feed'
  const go = (value: Screen) =>
    // Push history (not replace) so the phone back button steps back to the feed.
    setSearchParams(value === 'feed' ? {} : { tab: value })
  // Turns waiting only on the fleet hold are the hold's banner's to explain, not
  // "stuck": they run the moment it is released.
  const stuckShown = stuck.filter((t) => t.kind !== 'hold')
  const fleetHeld = fleetHold?.held ?? false
  const alertCount = (openAlerts?.length ?? 0) + (fleetHeld ? 1 : 0)

  // One runner, by link: `?tab=runners&runner=<id>` (held in mail and old alerts)
  // opens that runner's own page, where every control for it lives.
  const navigate = useNavigate()
  const runnerParam = searchParams.get('runner')
  useEffect(() => {
    const hit = runnerParam ? runners?.find((r) => r.id === runnerParam) : undefined
    if (hit?.workspace) navigate(runnerHref(hit), { replace: true })
  }, [runnerParam, runners, navigate])
  const { workspaces, active: activeWorkspace } = useWorkspace()
  const activeWorkspaceName =
    workspaces.find((w) => w.slug === activeWorkspace)?.display_name ?? activeWorkspace ?? ''
  // Changing a task's fields in place needs the editor role (`agent.work` in
  // its workspace); deciding it (in the peek) is the server's call per task.
  const canEditTask = (t: TaskOut): boolean =>
    roleAllows(workspaces.find((w) => w.slug === t.workspace)?.role, 'agent.work')

  const screen = SCREENS.find((s) => s.id === tab)
  const badgeFor = (id: Screen): number => (id === 'waiting' ? totalWaiting : id === 'runners' ? alertCount : 0)
  const menuBadge = totalWaiting + alertCount

  return (
    // `max-w-2xl` (672px) is the right measure for the feed and Waiting on you, whose
    // cards are prose you read. The runner and session tables get the room from
    // `lg` up — on a 1440 laptop (one of this surface's three consumers, beside
    // the phone PWA and the menubar) a 672px table leaves half the window empty.
    <div
      className={`mx-auto flex w-full flex-col gap-4 p-4 ${tab === 'feed' || tab === 'waiting' ? 'max-w-2xl' : 'max-w-2xl lg:max-w-5xl'}`}
      data-testid="supervisor-page"
    >
      <header className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          {tab === 'feed' ? (
            <>
              <h1 className="text-lg font-semibold text-foreground">Supervisor</h1>
              <p className="mt-0.5 text-[12px] text-muted-foreground">Sessions waiting for your next prompt.</p>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={() => go('feed')}
                className="text-[12px] text-muted-foreground hover:text-foreground"
                data-testid="back-to-feed"
              >
                ← Feed
              </button>
              <h1 className="text-lg font-semibold text-foreground">{screen?.label}</h1>
            </>
          )}
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger
            render={<Button variant="outline" size="sm" aria-label="Supervisor menu" data-testid="supervisor-menu" />}
          >
            <Menu className="h-4 w-4" />
            {menuBadge > 0 && (
              <span className="ml-1 rounded bg-primary/15 px-1.5 text-[11px] font-medium text-primary">{menuBadge}</span>
            )}
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-44">
            <DropdownMenuItem onClick={() => go('feed')} data-testid="menu-feed">
              Feed
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            {SCREENS.filter((s) => s.id !== 'feed').map((s) => (
              <DropdownMenuItem key={s.id} onClick={() => go(s.id)} data-testid={`menu-${s.id}`}>
                <span className="flex-1">{s.label}</span>
                {badgeFor(s.id) > 0 && (
                  <span className="ml-3 rounded bg-primary/15 px-1.5 text-[11px] font-medium text-primary">
                    {badgeFor(s.id)}
                  </span>
                )}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </header>

      {/* Feed — finished sessions waiting for the next prompt. Open alerts
          get ONE line here pointing at Runners, where they are listed: the feed
          is for moving work forward, but a problem to fix must not go unseen. */}
      {tab === 'feed' && (
        <>
          {/* A box waiting on you to approve a sign-in — the push's twin. */}
          <SignInRequests />
          {/* An agent asking to act as you on canopy — the push's twin too. */}
          <ActAsRequests />
          {alertCount > 0 && (
            <button
              type="button"
              role="alert"
              onClick={() => go('runners')}
              data-testid="feed-fleet-alert"
              className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-left text-[12px] font-medium text-warning hover:bg-warning/15"
            >
              {fleetHeld
                ? '⏸ Fleet on hold — no runner starts anything. View runners →'
                : `⚠ ${alertCount} fleet alert${alertCount === 1 ? '' : 's'} to fix. View runners →`}
            </button>
          )}
          {totalWaiting > 0 && (
            <button
              type="button"
              onClick={() => go('waiting')}
              data-testid="feed-waiting-link"
              className="rounded-lg border border-border bg-card px-3 py-2 text-left text-[12px] text-foreground-secondary hover:bg-muted"
            >
              {totalWaiting} task{totalWaiting === 1 ? '' : 's'} waiting on you — reviews and questions →
            </button>
          )}
          <SessionFeed agents={agents} runners={renderRunners} />
        </>
      )}

      {/* Waiting on you — the fleet's tasks with an open ask or parked on you,
          as the shared task table; a title opens the task beside it. */}
      {tab === 'waiting' &&
        (errs.waiting ? (
          <BandError message={errs.waiting} />
        ) : waiting === null ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <WaitingOnYou
            tasks={waiting}
            canEdit={canEditTask}
            onChanged={reloadWaiting}
          />
        ))}

      {/* Sessions — ONE unified list (web-started + runner-discovered). Every row
          opens into the streaming ChatPanel; "New chat with <agent> or project"
          is the creation entry point. */}
      {tab === 'sessions' && <ChatSessionsPanel agents={agents ?? undefined} heading="All sessions" />}

      {/* Agents — fleet KPIs + the one-time setup prompts. */}
      {tab === 'agents' && (
        <div className="flex flex-col gap-4">
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
        </div>
      )}

      {/* Runners — the alerts you can act on, queued turns, fleet runner health, and per-runner detail. */}
      {tab === 'runners' && (
        <div className="flex flex-col gap-4">
          {/* The fleet hold: a superuser's control, and everyone's banner while on. */}
          {/* This workspace's controls (owners and workspace superusers), then the system's. */}
          <WorkspaceControlsCard slug={activeWorkspace} name={activeWorkspaceName} />
          <FleetHoldCard hold={fleetHold} onChange={setFleetHold} />
          <OperatorsCard show={fleetHold?.can_stop_running ?? false} />

          <AlertList title="Alerts" hideEmpty />

          <QueuedTurns
            turns={stuckShown}
            onCancelled={(ids) => setStuck((prev) => (prev ?? []).filter((t) => !ids.includes(t.turn_id)))}
          />

          {errs.runners ? (
            <BandError message={errs.runners} />
          ) : renderRunners === null ? (
            <Skeleton className="h-12 w-full" />
          ) : (
            <RunnerStatus runners={renderRunners} onSelect={(r) => navigate(runnerHref(r))} />
          )}
        </div>
      )}
    </div>
  )
}
