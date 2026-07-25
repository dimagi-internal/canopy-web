import { useCallback, useEffect, useMemo, useState, type JSX } from 'react'
import { useSearchParams } from 'react-router-dom'
import { listAgents, type AgentOut } from '@/api/agents'
import { listOpenItems, type ItemOut } from '@/api/items'
import { listRunners, type RunnerOut } from '@/api/harness'
import { useLiveSupervisor } from '@/hooks/useLiveSupervisor'
import { RunnerStatus } from '@/components/supervisor/RunnerStatus'
import { RunnerDetail } from '@/components/supervisor/RunnerDetail'
import { AgentKpiCard } from '@/components/supervisor/AgentKpiCard'
import { ItemInbox } from '@/components/supervisor/ItemInbox'
import { ChatSessionsPanel } from '@/components/chat/ChatSessionsPanel'
import { RunnerAssignments } from '@/components/agents/RunnerAssignments'
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

  // Re-poll runners so the wrong-branch alert (below) appears/clears without a reload
  // — code_branch rides the REST runner, not the live socket overlay.
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
  // means Inbox (what push targets).
  const tab =
    raw === 'sessions' || raw === 'agents' || raw === 'runners' || raw === 'routing' ? raw : 'inbox'
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

      {/* LOUD alert: a runner on any branch but main is silently running stale/wrong
          code (usually another process checked out a branch in its shared checkout). */}
      {(renderRunners ?? [])
        .filter((r) => r.code_branch && r.code_branch !== 'main')
        .map((r) => (
          <div
            key={`branch-alert-${r.id}`}
            role="alert"
            data-testid={`runner-branch-alert-${r.id}`}
            className="rounded-lg border-2 border-destructive bg-destructive/15 p-3 text-destructive"
          >
            <p className="text-[13px] font-bold uppercase tracking-wide">⚠ Runner on wrong branch — stale code</p>
            <p className="mt-1 text-[13px] leading-snug">
              <span className="font-semibold">{r.name}</span> is running on branch{' '}
              <span className="rounded bg-destructive/20 px-1 font-mono font-semibold">{r.code_branch}</span>, not{' '}
              <span className="font-mono">main</span>. Its turns are executing{' '}
              <span className="font-semibold">stale / wrong code</span> — another process likely checked out a
              branch in the runner's checkout.
            </p>
            <p className="mt-1.5 break-words text-[12px] leading-snug opacity-90">
              Fix on that machine, then restart the runner:
              <br />
              <span className="font-mono">git -C ~/emdash-projects/canopy-web checkout main &amp;&amp; git pull</span>
            </p>
          </div>
        ))}

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
          <TabsTrigger value="routing" data-testid="tab-routing">
            Routing
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
            />
          ) : errs.runners ? (
            <BandError message={errs.runners} />
          ) : renderRunners === null ? (
            <Skeleton className="h-12 w-full" />
          ) : (
            <RunnerStatus runners={renderRunners} onSelect={setSelectedRunner} />
          )}
        </TabsContent>

        {/* Routing — the matrix: one ranked runner-assignment row per agent. */}
        <TabsContent value="routing" className="flex flex-col gap-4">
          {errs.agents ? (
            <BandError message={errs.agents} />
          ) : agents === null ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : agents.length === 0 ? (
            <p className="text-[13px] text-muted-foreground">No agents.</p>
          ) : (
            <div className="flex flex-col gap-2" data-testid="routing-matrix">
              {agents.map((a) => (
                <div key={a.slug} className="flex items-center gap-3">
                  <span className="w-24 shrink-0 truncate text-[12px] font-medium text-foreground" title={a.name}>
                    {a.name}
                  </span>
                  <RunnerAssignments agentSlug={a.slug} />
                </div>
              ))}
            </div>
          )}
        </TabsContent>
      </Tabs>
    </div>
  )
}
