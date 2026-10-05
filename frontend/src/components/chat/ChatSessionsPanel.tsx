import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { closeSession, listSessions, type ChatSession, type SessionState } from '@/api/chat'
import { listAgents, type AgentOut } from '@/api/agents'
import { projectsApi, type ProjectSlug } from '@/api/projects'
import { relativeTime } from '@/components/activity/turnLog'
import { sessionTargetLabel } from './sessionTargetLabel'
import { sessionDisplayTitle } from './sessionDisplayTitle'
import { projectHeader, sortSessions, type SessionSort } from './sessionSort'
import { closeIntent, closeResultMessage, settleClosing } from './closeAction'
import { parkedReason, parkedSummary, partitionByRunnerReachability } from './runnerEligibility'
import { NewChatMenu } from './NewChatMenu'
import { TransferSessionMenu } from './TransferSessionMenu'
import type { TransferResult } from '@/api/chat'

/** An independent on/off filter — deliberately shaped unlike the sort segments
 *  beside it, so the row does not read as four options where you pick one. */
function FilterToggle({
  checked,
  onChange,
  testId,
  children,
}: {
  checked: boolean
  onChange: () => void
  testId?: string
  children: ReactNode
}) {
  return (
    <label
      className={`inline-flex min-h-11 cursor-pointer items-center gap-1.5 sm:min-h-0 ${
        checked ? 'text-foreground' : 'text-muted-foreground hover:text-foreground-secondary'
      }`}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={onChange}
        data-testid={testId}
        className="h-3.5 w-3.5 shrink-0 accent-primary"
      />
      {children}
    </label>
  )
}

/**
 * Reusable, CROSS-WORKSPACE chat session surface: a findable list of your chat
 * sessions (continue any from any device) + "New chat with <agent>". Each session
 * links to ITS OWN workspace's chat route, and a new chat is created in the chosen
 * agent's workspace — the fleet spans workspaces. Used by the standalone chat home
 * (/w/:ws/chat) and by the root-scoped supervisor Sessions tab, which embeds this
 * as its single unified session list (no separate grouped-by-project view).
 */
export function ChatSessionsPanel({
  agents: agentsProp,
  heading = 'Chats',
}: {
  agents?: AgentOut[]
  heading?: string
}) {
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [agents, setAgents] = useState<AgentOut[]>(agentsProp ?? [])
  const [projects, setProjects] = useState<ProjectSlug[]>([])
  const [loading, setLoading] = useState(true)
  const [sort, setSort] = useState<SessionSort>('time')
  const [showArchived, setShowArchived] = useState(false)
  // Sessions whose bound runner is paused or offline are held back by default:
  // a message sent to one does not fail, it sits QUEUED until that box returns,
  // so listing them beside live chats invites a send that silently goes nowhere.
  // Held back, not dropped — a pause is temporary by construction, and the
  // conversation is still there. This toggle is how you get to it.
  const [showOffline, setShowOffline] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [closingId, setClosingId] = useState<string | null>(null)
  // Closes relayed to a runner and not yet confirmed: session id -> when.
  const [pendingClose, setPendingClose] = useState<Record<string, number>>({})
  const [closeError, setCloseError] = useState<string | null>(null)
  // Set on every transfer attempt, success or failure — a "moved" result is
  // worth saying too, since the row's own runner name updates silently on the
  // next poll and a click that visibly did nothing reads as a dropped click.
  const [transferNote, setTransferNote] = useState<{ text: string; error: boolean } | null>(null)


  useEffect(() => {
    if (agentsProp) setAgents(agentsProp)
  }, [agentsProp])

  useEffect(() => {
    let live = true
    setLoading(true)
    // Sessions + projects always load (projects feed the "+ New chat" dropdown);
    // agents load unless provided by a prop.
    const jobs: Promise<unknown>[] = [listSessions(showArchived ? 'all' : 'active'), projectsApi.listSlugs()]
    if (!agentsProp) jobs.push(listAgents({ limit: 100 }))
    Promise.allSettled(jobs).then((results) => {
      if (!live) return
      const [s, p, a] = results
      if (s.status === 'fulfilled') setSessions(s.value as ChatSession[])
      else setError(s.reason instanceof Error ? s.reason.message : 'failed to load sessions')
      if (p.status === 'fulfilled') setProjects(p.value as ProjectSlug[])
      if (!agentsProp && a && a.status === 'fulfilled') {
        setAgents((a.value as { items: AgentOut[] }).items)
      }
      setLoading(false)
    })
    return () => {
      live = false
    }
  }, [agentsProp, showArchived])

  // A slow REST refresh keeps the unified list current (the live push into the
  // list is a deferred follow-up; per-row liveness is live inside ChatPanel).
  useEffect(() => {
    const state: SessionState = showArchived ? 'all' : 'active'
    const id = window.setInterval(() => {
      listSessions(state)
        .then(setSessions)
        .catch(() => { /* keep last-good; the mount fetch owns first-error surfacing */ })
    }, 20_000)
    return () => window.clearInterval(id)
  }, [showArchived])

  const agentName = useMemo(() => {
    const by = new Map(agents.map((a) => [a.slug, a.name]))
    return (slug: string | null) => (slug ? by.get(slug) ?? slug : null)
  }, [agents])

  // The row is NOT removed here. `closing: true` means the close was relayed
  // and the emdash task is still the truth: the row leaves once the runner's
  // report has actually retired it. Removing it optimistically would be a lie
  // whenever the delete failed. So it is MARKED instead ("Closing…") and the
  // list re-checks every 2s until it goes (see settleClosing).
  const onClose = useCallback(
    async (s: ChatSession) => {
      const intent = closeIntent(s)
      if (intent.kind === 'blocked') return
      if (
        intent.confirm &&
        !window.confirm(`${s.title?.trim() || 'This chat'} is still working. Close it anyway?`)
      ) {
        return
      }
      setClosingId(s.id)
      setCloseError(null)
      try {
        const result = await closeSession(s.id)
        const message = closeResultMessage(result, s)
        if (message) setCloseError(message)
        else if (result.closing) setPendingClose((prev) => ({ ...prev, [s.id]: Date.now() }))
        else setSessions(await listSessions(showArchived ? 'all' : 'active'))
      } catch {
        setCloseError('Couldn’t close this session')
      } finally {
        setClosingId(null)
      }
    },
    [showArchived],
  )

  // `result` is null on failure (the message is the error instead). A "moved"
  // result refreshes the list so the row's runner name is current at once
  // rather than waiting for the 20s poll; "pending" leaves the row as-is —
  // nothing moved, a request is just waiting on an approver now.
  const onTransferred = useCallback(
    (result: TransferResult | null, error: string | null) => {
      if (error) {
        setTransferNote({ text: error, error: true })
        return
      }
      if (result?.status === 'pending') {
        setTransferNote({
          text: `Transfer requested — waiting on ${result.approvers.join(', ') || 'the runner’s administrator'} to approve.`,
          error: false,
        })
        return
      }
      setTransferNote({ text: `Moved to ${result?.runner}.`, error: false })
      void listSessions(showArchived ? 'all' : 'active').then(setSessions)
    },
    [showArchived],
  )

  // Watch the relayed closes through to the end, quickly, rather than leaving
  // them to the 20s refresh.
  const waitingOnCloses = Object.keys(pendingClose).length > 0
  useEffect(() => {
    if (!waitingOnCloses) return
    const state: SessionState = showArchived ? 'all' : 'active'
    const tick = window.setInterval(() => {
      listSessions(state)
        .then((listed) => {
          setSessions(listed)
          setPendingClose((prev) => {
            const { pending, stuck } = settleClosing(prev, listed, Date.now())
            if (stuck.length > 0) {
              const names = listed.filter((x) => stuck.includes(x.id)).map((x) => x.title?.trim() || 'A chat')
              setCloseError(
                `${names.join(', ')} ${names.length === 1 ? 'is' : 'are'} still open: its runner has not confirmed the close. Try again, or close it in emdash.`,
              )
            }
            return pending
          })
        })
        .catch(() => { /* keep waiting; the next tick retries */ })
    }, 2_000)
    return () => window.clearInterval(tick)
  }, [waitingOnCloses, showArchived])


  const now = new Date()

  // Split before sorting so the hidden count is honest about the whole list,
  // not about whatever survived the current sort.
  const { live: liveSessions, parked } = useMemo(
    () => partitionByRunnerReachability(sessions),
    [sessions],
  )
  const visible = showOffline ? sessions : liveSessions
  // Counted over the WHOLE list, not `visible`: a session waiting on a human is
  // the one thing you want to know is there even when the current filter is
  // hiding it. Sits beside the heading so it reads from a collapsed tab.
  const waitingCount = sessions.filter((s) => s.waiting_on_you).length

  return (
    // Named so a test can assert this tab is (or is not) showing. The supervisor
    // spec used to reach for `open-sessions`, which belonged to the composer UI
    // this panel replaced.
    <div className="flex min-h-0 flex-col" data-testid="sessions-panel">
      <div className="flex items-center justify-between gap-2 pb-2">
        <h2 className="flex items-center gap-1.5 text-sm font-semibold text-foreground">
          {heading}
          {waitingCount > 0 && (
            <span className="rounded bg-warning/15 px-1.5 py-0.5 text-[11px] font-medium text-warning">
              {waitingCount} waiting on you
            </span>
          )}
        </h2>
        <NewChatMenu agents={agents} projects={projects} onError={setError} />
      </div>

      {/* `parked.length` opens the toolbar too: with one session, and that one
          held back, the sort row would never render and its own reveal toggle
          would be unreachable — a chat you cannot get back to. */}
      {(sessions.length > 1 || showArchived || parked.length > 0 || showOffline) && (
        // Four identical pills in a row, but the first two are a choose-ONE sort
        // and the last two are independent switches — a difference carried only
        // by the word "Sort" at the far left, which governs half the row. The
        // sort is now one segmented control with a shared border (visibly one
        // widget, one choice); the filters are checkboxes that say on or off.
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 pb-2 text-xs">
          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground">Sort</span>
            <div className="inline-flex overflow-hidden rounded-md border border-border" role="group">
              {(['time', 'project'] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setSort(m)}
                  aria-pressed={sort === m}
                  className={`min-h-11 px-2.5 py-0.5 sm:min-h-0 ${
                    sort === m
                      ? 'bg-primary/10 font-medium text-primary'
                      : 'text-muted-foreground hover:bg-muted'
                  }`}
                >
                  {m === 'time' ? 'Recent' : 'Project'}
                </button>
              ))}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <FilterToggle checked={showArchived} onChange={() => setShowArchived((v) => !v)}>
              Show archived
            </FilterToggle>
            {(parked.length > 0 || showOffline) && (
              <FilterToggle
                checked={showOffline}
                onChange={() => setShowOffline((v) => !v)}
                testId="toggle-offline"
              >
                Show offline
              </FilterToggle>
            )}
          </div>
        </div>
      )}

      {error && <div className="py-2 text-sm text-destructive">{error}</div>}
      {closeError && <div className="py-2 text-sm text-destructive">{closeError}</div>}
      {transferNote && (
        <div className={`py-2 text-sm ${transferNote.error ? 'text-destructive' : 'text-foreground-secondary'}`}>
          {transferNote.text}
        </div>
      )}
      {loading ? (
        <div className="py-6 text-sm text-muted-foreground">Loading sessions…</div>
      ) : visible.length === 0 ? (
        parked.length > 0 ? (
          <div className="flex flex-col items-center justify-center gap-1 py-12 text-center">
            <div className="text-sm text-foreground">No chats on a live runner</div>
            <div className="text-xs text-muted-foreground">{parkedSummary(parked)}</div>
          </div>
        ) : (
          <div className="rounded-lg border border-border bg-card p-4">
            <h2 className="text-sm font-semibold text-foreground">No chats yet</h2>
            <p className="mt-2 text-[13px] leading-relaxed text-foreground-secondary">
              Start a chat to hand an agent a job. Sending a message enqueues a turn, and a runner
              picks it up and streams the reply back as it works.
            </p>
            <p className="mt-2 text-[12px] text-muted-foreground">
              {agents.length === 0 && projects.length === 0 ? (
                <>
                  Nothing to chat with?{' '}
                  <Link to="/guide#/w/:workspace/agents" className="text-primary hover:underline">
                    You need an agent first
                  </Link>
                  .
                </>
              ) : (
                <>Pick one from "New chat with…" above to start one.</>
              )}
            </p>
          </div>
        )
      ) : (
        <ul className="divide-y divide-border rounded-md border border-border">
          {sortSessions(visible, sort).map((s, i, rows) => {
            const label = sessionTargetLabel(agentName(s.agent_slug), s.project ?? '')
            const header = projectHeader(rows, i, sort)
            const parkedWhy = parkedReason(s)
            const intent = closeIntent(s)
            return (
              <li key={s.id}>
                {header && (
                  <div className="bg-muted/40 px-3 py-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    {header}
                  </div>
                )}
                {/* Link and Close are SIBLINGS: a button inside an anchor is
                    invalid HTML and the anchor eats the click. */}
                <div className="flex items-stretch">
                  <Link
                    to={`/w/${s.workspace}/chat/${s.id}`}
                    data-testid={parkedWhy ? `session-parked-${s.id}` : undefined}
                    className={`flex min-w-0 flex-1 items-center justify-between gap-3 px-3 py-2.5 hover:bg-muted${
                      parkedWhy || s.id in pendingClose ? ' opacity-60' : ''
                    }`}
                  >
                    <div className="min-w-0">
                      <div
                        className="truncate text-sm font-medium text-foreground"
                        title={s.title?.trim() || undefined}
                      >
                        {sessionDisplayTitle(s.title) || 'Untitled chat'}
                      </div>
                      <div className="truncate text-xs text-muted-foreground">
                        {label} · {s.workspace}
                        {s.origin === 'runner' ? ' · discovered' : ''}
                        {s.status !== 'active' ? ` · ${s.status}` : ''}
                      </div>
                    </div>
                    <div className="flex shrink-0 flex-col items-end gap-0.5 text-xs">
                      {/* Why this row is dimmed. Sits where `running` would, because
                          it answers the same question — can this chat act right now. */}
                      {parkedWhy ? (
                        <span className="rounded bg-muted px-1 text-[10px] text-muted-foreground">
                          runner {parkedWhy}
                        </span>
                      ) : s.waiting_on_you ? (
                        /* Outranks `running`: an agent blocked on a dialog is the
                           one row here you can do something about, and it is the
                           one that otherwise reads as merely quiet — a waiting
                           session stops writing, so it sinks in a list ordered by
                           activity and looks identical to an idle one. */
                        <span className="flex items-center gap-1 font-medium text-warning">
                          <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-warning" />
                          waiting on you
                        </span>
                      ) : s.running ? (
                        <span className="flex items-center gap-1 font-medium text-success">
                          <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
                          running
                        </span>
                      ) : (
                        <span className="flex items-center gap-1 text-muted-foreground">
                          <span className="inline-block h-1.5 w-1.5 rounded-full bg-muted-foreground" />
                          idle
                        </span>
                      )}
                      {/* The age used to REPLACE the status, so one slot held two
                          different kinds of fact — five rows read "running" and
                          the sixth read "6h ago", and the column could not be
                          scanned without reading each value to learn which
                          question it was answering. Status always; age alongside. */}
                      <span className="text-muted-foreground">{relativeTime(s.last_activity_at, now)}</span>
                      {s.runner_name && (
                        <span className="text-muted-foreground">
                          {s.runner_name}
                          {s.runner_location ? ` · ${s.runner_location}` : ''}
                        </span>
                      )}
                    </div>
                  </Link>
                  <TransferSessionMenu session={s} onResult={onTransferred} />
                  <button
                    type="button"
                    data-testid={`close-session-${s.id}`}
                    aria-label={`Close ${s.title?.trim() || 'Untitled chat'}`}
                    title={
                      intent.kind === 'blocked'
                        ? intent.why
                        : 'Close this session (deletes its emdash task)'
                    }
                    disabled={intent.kind === 'blocked' || closingId === s.id || s.id in pendingClose}
                    onClick={() => void onClose(s)}
                    className={
                      s.id in pendingClose
                        ? 'shrink-0 px-3 text-[12px] italic text-muted-foreground'
                        : 'shrink-0 px-3 text-muted-foreground hover:text-destructive disabled:opacity-40'
                    }
                  >
                    {s.id in pendingClose ? 'Closing…' : closingId === s.id ? '…' : '×'}
                  </button>
                </div>
              </li>
            )
          })}
        </ul>
      )}
      {/* Say what is being withheld. A filtered list that does not admit it is
          filtering reads as "that chat is gone". */}
      {!loading && !showOffline && parked.length > 0 && visible.length > 0 && (
        <button
          type="button"
          onClick={() => setShowOffline(true)}
          data-testid="parked-summary"
          // It always revealed them on click; it just did not look like it could.
          // Set smaller and dimmer than the rows it withholds, with no border or
          // underline, it read as a caption explaining an absence rather than a
          // control that undoes it — while hiding 10 of 16 chats.
          className="mt-1 min-h-11 self-start rounded-md border border-dashed border-border px-2 py-1.5 text-xs text-foreground-secondary transition-colors hover:border-input hover:bg-muted hover:text-foreground sm:min-h-0"
        >
          {parkedSummary(parked)} <span className="text-primary">· Show them</span>
        </button>
      )}
    </div>
  )
}

export default ChatSessionsPanel
