import { useCallback, useEffect, useMemo, useState, type JSX } from 'react'
import { useSearchParams } from 'react-router-dom'
import { listAgents, type AgentOut } from '@/api/agents'
import { listOpenItems, type ItemOut } from '@/api/items'
import { listRunners, listUnclaimableTurns, retireRunner, type RunnerOut, type UnclaimableTurn } from '@/api/harness'
import { RunnerCodeAlerts } from '@/components/supervisor/RunnerCodeAlerts'
import { useLiveSupervisor } from '@/hooks/useLiveSupervisor'
import { RunnerStatus } from '@/components/supervisor/RunnerStatus'
import { RunnerDetail } from '@/components/supervisor/RunnerDetail'
import { AgentKpiCard } from '@/components/supervisor/AgentKpiCard'
import { ItemInbox } from '@/components/supervisor/ItemInbox'
import { ChatSessionsPanel } from '@/components/chat/ChatSessionsPanel'
import { InstallPrompt } from '@/pwa/InstallPrompt'
import { PushToggle } from '@/pwa/PushToggle'
import { setBadge } from '@/pwa/usePush'
import { Skeleton, Tabs, TabsList, TabsTrigger, TabsContent } from 'canopy-ui'

function BandError({ message }: { message: string }): JSX.Element {
  return (
    <p className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-[13px] text-destructive">
      {message}
    </p>
  )
}

// The ONE supervisor surface (spec 2026-07-14). Three consumers will load this
// same route: the phone as an installed PWA, the menubar's WKWebView (Phase 5),
// and a desktop browser. Phone-first layout — a single column that widens.
export default function SupervisorPage(): JSX.Element {
  const [agents, setAgents] = useState<AgentOut[] | null>(null)
  const [runners, setRunners] = useState<RunnerOut[] | null>(null)
  const [items, setItems] = useState<ItemOut[] | null>(null)
  const [selectedRunner, setSelectedRunner] = useState<RunnerOut | null>(null)
  // Per-band errors, not one page-level error: on cellular a single flaky call
  // is the common case, and Promise.all would blank all three bands for it.
  const [errs, setErrs] = useState<{ agents?: string; runners?: string; items?: string }>({})

  // Reloadable on its own so acting on an item (decide/dismiss) refreshes the
  // inbox without refetching agents + runners.
  const reloadItems = useCallback(() => {
    listOpenItems()
      .then((rows) => {
        setItems(rows)
        setErrs((e) => ({ ...e, items: undefined }))
      })
      .catch((err: unknown) =>
        setErrs((e) => ({ ...e, items: err instanceof Error ? err.message : 'Failed to load' })),
      )
  }, [])

  useEffect(() => {
    let cancelled = false
    const msg = (r: PromiseRejectedResult) =>
      r.reason instanceof Error ? r.reason.message : 'Failed to load'

    Promise.allSettled([listAgents({ limit: 100 }), listRunners(), listOpenItems()]).then(
      ([a, r, f]) => {
        if (cancelled) return
        if (a.status === 'fulfilled') setAgents(a.value.items)
        else setErrs((e) => ({ ...e, agents: msg(a) }))
        if (r.status === 'fulfilled') setRunners(r.value)
        else setErrs((e) => ({ ...e, runners: msg(r) }))
        if (f.status === 'fulfilled') setItems(f.value)
        else setErrs((e) => ({ ...e, items: msg(f) }))
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
  // else derive from the fetched open items.
  const itemCountFor = (slug: string): number =>
    (items ?? []).filter((i) => i.agent_slug === slug).length
  const waitingFor = (slug: string): number =>
    live.hasSnapshot && slug in live.waiting ? live.waiting[slug] : itemCountFor(slug)
  const liveTotalWaiting = Object.values(live.waiting).reduce((a, b) => a + b, 0)
  const totalWaiting = live.hasSnapshot ? liveTotalWaiting : (items?.length ?? 0)

  // The app-icon count. Android honours this; elsewhere it no-ops.
  useEffect(() => {
    if (live.hasSnapshot || items) setBadge(totalWaiting)
  }, [live.hasSnapshot, items, totalWaiting])

  const [searchParams, setSearchParams] = useSearchParams()
  const raw = searchParams.get('tab')
  // Unknown / absent value falls back to Inbox — never a blank tab, and no param
  // means Inbox (what push targets). Routing edits moved onto the Runners tab
  // (RunnerDetail's per-agent RunnerAssignments editor) — there is no
  // standalone Routing tab anymore.
  const tab = raw === 'sessions' || raw === 'agents' || raw === 'runners' ? raw : 'inbox'
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

      {/* LOUD alert: a queued turn addressed to an agent/repo NOTHING online declares
          sits forever with no signal (one sat 12h). Make the stall visible. */}
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
              : 'Declare it on a runner (Runners tab) or cancel the turn — it will not run otherwise.'}
          </p>
        </div>
      )}

      <RunnerCodeAlerts runners={renderRunners} retiringId={retiring} onRetire={handleRetire} />

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
          <TabsTrigger value="runners" data-testid="tab-runners">
            Runners
          </TabsTrigger>
        </TabsList>

        {/* Inbox — the fleet's open items, actionable in place (the act-now surface). */}
        <TabsContent value="inbox" className="flex flex-col gap-3">
          {errs.items ? (
            <BandError message={errs.items} />
          ) : items === null ? (
            <Skeleton className="h-24 w-full" />
          ) : (
            <ItemInbox items={items} onActed={reloadItems} />
          )}
        </TabsContent>

        {/* Sessions — ONE unified list (web-started + runner-discovered are the same
            Session now). Every row opens into the streaming ChatPanel; "New chat with
            <agent> or project" stays as the creation entry point. */}
        <TabsContent value="sessions" className="flex flex-col gap-4">
          <ChatSessionsPanel agents={agents ?? undefined} heading="Sessions" />
        </TabsContent>

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
              onBack={() => setSelectedRunner(null)}
              onChanged={handleRunnerChanged}
            />
          ) : errs.runners ? (
            <BandError message={errs.runners} />
          ) : renderRunners === null ? (
            <Skeleton className="h-12 w-full" />
          ) : (
            <RunnerStatus runners={renderRunners} onSelect={setSelectedRunner} />
          )}
        </TabsContent>
      </Tabs>
    </div>
  )
}
