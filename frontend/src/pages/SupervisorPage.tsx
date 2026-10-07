import { useCallback, useEffect, useMemo, useState, type JSX } from 'react'
import { useSearchParams } from 'react-router-dom'
import { listAgents, type AgentOut, type TaskOut } from '@/api/agents'
import { listRunners, listUnclaimableTurns, retireRunner, type RunnerOut, type UnclaimableTurn } from '@/api/harness'
import { Menu } from 'lucide-react'
import {
  Button,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from 'canopy-ui/ui'
import { RunnerAlerts } from '@/components/supervisor/RunnerAlerts'
import { runnerAlerts } from '@/components/supervisor/runnerAlertRules'
import { SessionFeed } from '@/components/supervisor/SessionFeed'
import { useLiveSupervisor } from '@/hooks/useLiveSupervisor'
import { RunnerStatus } from '@/components/supervisor/RunnerStatus'
import { RunnerDetail } from '@/components/supervisor/RunnerDetail'
import { AgentKpiCard } from '@/components/supervisor/AgentKpiCard'
import { WaitingOnYou, loadWaitingOnYou } from '@/components/supervisor/WaitingOnYou'
import { ChatSessionsPanel } from '@/components/chat/ChatSessionsPanel'
import { InstallPrompt } from '@/pwa/InstallPrompt'
import { PushToggle } from '@/pwa/PushToggle'
import { setBadge } from '@/pwa/usePush'
import { Skeleton } from 'canopy-ui'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { roleAllows } from '@/lib/workspaceRoles'

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
  const [selectedRunner, setSelectedRunner] = useState<RunnerOut | null>(null)
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

    Promise.allSettled([listAgents({ limit: 100 }), listRunners(), loadWaitingOnYou()]).then(
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
  // declaring a repo on a runner is what clears them.
  const [stuck, setStuck] = useState<UnclaimableTurn[]>([])
  useEffect(() => {
    let cancelled = false
    const load = () =>
      listUnclaimableTurns()
        .then((t) => { if (!cancelled) setStuck(t) })
        .catch(() => { /* non-fatal: the banner is a warning, not a gate */ })
    load()
    const id = window.setInterval(load, 30_000)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [])

  // The wrong-branch banner's one-click resolve for a dead runner. Confirmed,
  // because retiring is permanent for the row (re-pairing mints a fresh one).
  const [retiring, setRetiring] = useState<string | null>(null)
  const handleRetire = useCallback(async (r: RunnerOut) => {
    if (!window.confirm(`Retire ${r.name}? This is permanent for this runner row — re-pairing later creates a new one.`)) return
    setRetiring(r.id)
    try {
      await retireRunner(r.id)
      // Drop it locally so the banner clears immediately; the 30s re-poll would
      // otherwise leave a confusing beat where the retired runner lingers.
      setRunners((prev) => prev?.filter((x) => x.id !== r.id) ?? prev)
    } catch (err) {
      setErrs((e) => ({ ...e, runners: err instanceof Error ? err.message : 'Retire failed' }))
    } finally {
      setRetiring(null)
    }
  }, [])

  // A pause/resume from the detail view. Patch both the list and the open detail
  // from the SERVER's row: the 30s re-poll would otherwise leave the button
  // showing the state you just left, which reads as "the tap didn't take".
  const handleRunnerChanged = useCallback((fresh: RunnerOut) => {
    setRunners((prev) => prev?.map((r) => (r.id === fresh.id ? fresh : r)) ?? prev)
    setSelectedRunner((prev) => (prev && prev.id === fresh.id ? fresh : prev))
  }, [])

  // Re-poll runners so the code-provenance alerts (below) appear/clear without a
  // reload — code_branch/code_sha ride the REST runner, not the live socket overlay.
  useEffect(() => {
    let cancelled = false
    const id = window.setInterval(() => {
      listRunners()
        .then((r) => { if (!cancelled) setRunners(r) })
        .catch(() => { /* keep last-good; the mount fetch owns first-error surfacing */ })
    }, 30_000)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [])

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
  const alertCount = runnerAlerts(renderRunners).length + stuck.length

  // One runner, by link: `?tab=runners&runner=<id>` opens its detail — what
  // Settings → Runners points at, so "where is this box's Claude login" has an
  // address. The selection writes the param back, so Back and a copied URL both
  // mean what is on screen.
  const runnerParam = searchParams.get('runner')
  useEffect(() => {
    if (!runnerParam || !runners || selectedRunner?.id === runnerParam) return
    const hit = runners.find((r) => r.id === runnerParam)
    if (hit) setSelectedRunner(hit)
  }, [runnerParam, runners, selectedRunner])
  // This tab is the live status view; configuring a box happens on the fleet map
  // (Settings → Topology), which needs logs.read in the runner's workspace — so
  // the link is offered only where it would open.
  const { workspaces } = useWorkspace()
  const mapHrefFor = (r: RunnerOut): string | undefined => {
    const role = workspaces.find((w) => w.slug === r.workspace)?.role
    return r.workspace && roleAllows(role, 'logs.read')
      ? `/w/${r.workspace}/settings/topology?runner=${r.id}`
      : undefined
  }
  // Dispatch / done need the agent's editor role (`agent.work` in its workspace);
  // approve, decline and reply are open to every viewer.
  const workspaceOf = useMemo(
    () => Object.fromEntries((agents ?? []).map((a) => [a.slug, a.workspace] as const)),
    [agents],
  )
  const canEditTask = (t: TaskOut): boolean =>
    roleAllows(workspaces.find((w) => w.slug === workspaceOf[t.agent_slug])?.role, 'agent.work')
  const selectRunner = (r: RunnerOut | null) => {
    setSelectedRunner(r)
    setSearchParams(r ? { tab: 'runners', runner: r.id } : { tab: 'runners' })
  }

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

      {/* Feed — finished sessions waiting for the next prompt. Fleet problems
          get ONE line here pointing at Runners, not the full banners: the feed
          is for moving work forward, but a stalled queue must not go unseen. */}
      {tab === 'feed' && (
        <>
          {alertCount > 0 && (
            <button
              type="button"
              role="alert"
              onClick={() => go('runners')}
              data-testid="feed-fleet-alert"
              className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-left text-[12px] font-medium text-warning hover:bg-warning/15"
            >
              ⚠ {alertCount} fleet alert{alertCount === 1 ? '' : 's'} — stuck turns or runner problems. View runners →
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
          <SessionFeed agents={agents} />
        </>
      )}

      {/* Waiting on you — the fleet's tasks with an open ask or parked on you,
          actionable in place. */}
      {tab === 'waiting' &&
        (errs.waiting ? (
          <BandError message={errs.waiting} />
        ) : waiting === null ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <WaitingOnYou tasks={waiting} canEdit={canEditTask} onChanged={reloadWaiting} />
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

      {/* Runners — fleet runner health, the LOUD alerts, and per-runner detail. */}
      {tab === 'runners' && (
        <div className="flex flex-col gap-4">
          {/* LOUD alert: a queued turn addressed to an agent/repo NOTHING online
              declares sits forever with no signal (one sat 12h). */}
          {stuck.length > 0 && (
            <div
              role="alert"
              data-testid="unclaimable-turns-alert"
              className="rounded-lg border-2 border-warning bg-warning/15 p-3 text-warning"
            >
              <p className="text-[13px] font-bold uppercase tracking-wide">
                {stuck.every((t) => t.kind === 'offline')
                  ? `⚠ ${stuck.length} queued turn${stuck.length === 1 ? '' : 's'} waiting on an unreachable runner`
                  : `⚠ ${stuck.length} queued turn${stuck.length === 1 ? '' : 's'} no runner can claim`}
              </p>
              <ul className="mt-1 space-y-1">
                {stuck.slice(0, 5).map((t) => (
                  <li key={t.turn_id} className="text-[13px] leading-snug">
                    <span className="rounded bg-warning/20 px-1 font-mono font-semibold">{t.target}</span>{' '}
                    {t.prompt ? <span className="opacity-90">“{t.prompt}”</span> : null}
                    <span className="block text-[12px] opacity-90">{t.reason}</span>
                  </li>
                ))}
              </ul>
              <p className="mt-1.5 text-[12px] leading-snug opacity-90">
                {stuck.every((t) => t.kind === 'offline')
                  ? 'These will run as soon as a runner reconnects — no action needed unless it stays.'
                  : 'Declare it on a runner (below) or cancel the turn — it will not run otherwise.'}
              </p>
            </div>
          )}

          <RunnerAlerts runners={renderRunners} retiringId={retiring} onRetire={handleRetire} />

          {selectedRunner ? (
            <RunnerDetail
              runner={selectedRunner}
              agents={agents ?? []}
              onBack={() => selectRunner(null)}
              onChanged={handleRunnerChanged}
              onRetired={(r) => {
                setRunners((prev) => prev?.filter((x) => x.id !== r.id) ?? prev)
                selectRunner(null)
              }}
              mapHref={mapHrefFor(selectedRunner)}
            />
          ) : errs.runners ? (
            <BandError message={errs.runners} />
          ) : renderRunners === null ? (
            <Skeleton className="h-12 w-full" />
          ) : (
            <RunnerStatus runners={renderRunners} onSelect={selectRunner} />
          )}
        </div>
      )}
    </div>
  )
}
