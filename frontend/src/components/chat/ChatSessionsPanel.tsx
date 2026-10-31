import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { clsx } from 'clsx'
import { closeSession, listSessions, type ChatSession, type SessionState } from '@/api/chat'
import { listMyAgents, type AgentOut } from '@/api/agents'
import { listRunners, recentRunners, type RunnerOut } from '@/api/harness'
import { rememberListed } from '@/pages/chat/sessionCache'
import { relativeAge } from '@/lib/relativeAge'
import { usePolled } from '@/hooks/usePolled'
import { sessionTargetLabel } from './sessionTargetLabel'
import { sessionDisplayTitle } from './sessionDisplayTitle'
import { ListTitle, listTitle } from './privateConversation'
import { projectHeader, sortSessions, type SessionSort } from './sessionSort'
import { closeFailure, closeIntent, closeResultMessage, settleClosing } from './closeAction'
import { CloseFailedNote } from './CloseFailedNote'
import { parkedSummary, partitionByRunnerReachability, repoChoices } from './runnerEligibility'
import { NewChatMenu } from './NewChatMenu'
import { TurnModeBadge } from './TurnModeBadge'
import { TransferSessionMenu } from './TransferSessionMenu'
import type { TransferResult } from '@/api/chat'
import { WorkspaceBadge } from '@/components/WorkspaceBadge'
import { StateDot } from '@/components/sessions/StateDot'
import { sessionDisplay } from '@/components/sessions/sessionDisplay'
import { scopedPath } from '../../lib/scopedLinks'

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

// "Started by me" outlives the page: it is how a busy person keeps the list
// readable, so it should not reset on every visit.
const MINE_ONLY_KEY = 'canopy.sessions.mineOnly'
function readMineOnly(): boolean {
  try {
    return window.localStorage.getItem(MINE_ONLY_KEY) === '1'
  } catch {
    return false
  }
}
function saveMineOnly(on: boolean) {
  try {
    window.localStorage.setItem(MINE_ONLY_KEY, on ? '1' : '0')
  } catch {
    // storage disabled — the choice lasts for this tab only
  }
}

// What this tab last listed, per workspace and filter. Opening a chat and coming back
// remounts the panel; it paints these rows at once and refreshes underneath,
// instead of "Loading sessions…" every time.
const listedBefore = new Map<string, ChatSession[]>()

// How old a fleet read another page made may be and still fill the
// "+ New chat" menu, which only needs the runners' declared repos.
const FLEET_REUSE_MS = 60_000

/** Tests only: start the next mount from nothing. */
export function forgetListed(): void {
  listedBefore.clear()
}

const listState = (archived: boolean): SessionState => (archived ? 'all' : 'active')
const listKey = (archived: boolean, workspace?: string) => `${workspace ?? ''}:${listState(archived)}`

/** The list as the server has it now, kept for the next mount, and each row
 *  marked so opening it skips the private-conversation read (as /ide's do).
 *  With a workspace, only that workspace's sessions (and those below it). */
function readList(archived: boolean, workspace?: string): Promise<ChatSession[]> {
  return listSessions(listState(archived), { workspace }).then((rows) => {
    listedBefore.set(listKey(archived, workspace), rows)
    rememberListed(rows)
    return rows
  })
}

/**
 * Reusable chat session surface: a findable list of your chat
 * sessions (continue any from any device) + "New chat with <agent>". Each session
 * links to ITS OWN workspace's chat route, and a new chat is created in the chosen
 * agent's workspace. Given a `workspace`, it lists that workspace's sessions —
 * the chat home (/w/:ws/sessions) is a page of one workspace; without one it spans
 * every workspace you are in, as the root-scoped supervisor Sessions tab does
 * (its single unified session list, no separate grouped-by-project view).
 */
export function ChatSessionsPanel({
  agents: agentsProp,
  heading = 'Sessions',
  workspace,
}: {
  agents?: AgentOut[]
  heading?: string
  workspace?: string
}) {
  const [sessions, setSessions] = useState<ChatSession[]>(() => listedBefore.get(listKey(false, workspace)) ?? [])
  const [agents, setAgents] = useState<AgentOut[]>(agentsProp ?? [])
  const [runners, setRunners] = useState<RunnerOut[] | null>(() => recentRunners(FLEET_REUSE_MS))
  const [loading, setLoading] = useState(() => !listedBefore.has(listKey(false, workspace)))
  const [sort, setSort] = useState<SessionSort>('time')
  const [showArchived, setShowArchived] = useState(false)
  // Sessions whose bound runner is paused or offline are held back by default:
  // a message sent to one does not fail, it sits QUEUED until that box returns,
  // so listing them beside live chats invites a send that silently goes nowhere.
  // Held back, not dropped — a pause is temporary by construction, and the
  // conversation is still there. This toggle is how you get to it.
  const [showOffline, setShowOffline] = useState(false)
  // Only the sessions you started (or that run on your own box) — others' chats
  // with your agents, and the agents' own runs, are left out.
  const [mineOnly, setMineOnly] = useState(readMineOnly)
  const toggleMineOnly = () =>
    setMineOnly((v) => {
      saveMineOnly(!v)
      return !v
    })
  const [error, setError] = useState<string | null>(null)
  const [closingId, setClosingId] = useState<string | null>(null)
  // Closes relayed to a runner and not yet confirmed: session id -> when.
  const [pendingClose, setPendingClose] = useState<Record<string, number>>({})
  const [closeError, setCloseError] = useState<string | null>(null)
  // Set on every transfer attempt, success or failure — a "moved" result is
  // worth saying too, since the row's own runner name updates silently on the
  // next poll and a click that visibly did nothing reads as a dropped click.
  const [transferNote, setTransferNote] = useState<{ text: string; error: boolean } | null>(null)


  // The list, the fleet and the agents load on their own: the list is the page
  // (~0.5s), while the fleet (1-3s) and the agents only feed the "+ New chat"
  // menu, so neither holds the rows back. A list read before paints at once.
  useEffect(() => {
    let live = true
    const before = listedBefore.get(listKey(showArchived, workspace))
    if (before) setSessions(before)
    setLoading(!before)
    readList(showArchived, workspace)
      .then((rows) => {
        if (live) setSessions(rows)
      })
      .catch((err: unknown) => {
        if (live && !before) setError(err instanceof Error ? err.message : 'failed to load sessions')
      })
      .finally(() => {
        if (live) setLoading(false)
      })
    return () => {
      live = false
    }
  }, [showArchived, workspace])

  useEffect(() => {
    if (recentRunners(FLEET_REUSE_MS)) return
    let live = true
    listRunners()
      .then((rows) => {
        if (live) setRunners(rows ?? [])
      })
      .catch(() => {
        if (live) setRunners([])
      })
    return () => {
      live = false
    }
  }, [])

  useEffect(() => {
    if (agentsProp) {
      setAgents(agentsProp)
      return
    }
    let live = true
    listMyAgents({ limit: 100 })
      .then((page) => {
        if (live) setAgents(page.items)
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [agentsProp])

  // A slow REST refresh keeps the unified list current (the live push into the
  // list is a deferred follow-up; per-row liveness is live inside ChatPanel).
  // A failed refresh keeps last-good; the mount fetch owns first-error surfacing.
  usePolled(
    () => readList(showArchived, workspace).then(setSessions).catch(() => {}),
    { ms: 20_000, deps: [showArchived, workspace], immediate: false },
  )

  const repos = useMemo(() => repoChoices(runners ?? []), [runners])

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
        !window.confirm(`${listTitle(s, s.title?.trim() || 'This chat')} is still working. Close it anyway?`)
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
        else setSessions(await readList(showArchived, workspace))
      } catch {
        setCloseError('Couldn’t close this session')
      } finally {
        setClosingId(null)
      }
    },
    [showArchived, workspace],
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
      setTransferNote({ text: `Moved to ${result?.runner}.${handoffNote(result)}`, error: false })
      void readList(showArchived, workspace).then(setSessions)
    },
    [showArchived, workspace],
  )

  // Watch the relayed closes through to the end, quickly, rather than leaving
  // them to the 20s refresh.
  const waitingOnCloses = Object.keys(pendingClose).length > 0
  usePolled(
    () =>
      readList(showArchived, workspace)
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
        .catch(() => { /* keep waiting; the next tick retries */ }),
    { ms: 2_000, deps: [showArchived, workspace], enabled: waitingOnCloses, immediate: false },
  )


  const now = new Date()

  // Split before sorting so the hidden count is honest about the whole list,
  // not about whatever survived the current sort.
  const scoped = useMemo(
    () => (mineOnly ? sessions.filter((s) => s.started_by_you) : sessions),
    [sessions, mineOnly],
  )
  const { live: liveSessions, parked } = useMemo(
    () => partitionByRunnerReachability(scoped),
    [scoped],
  )
  const visible = showOffline ? scoped : liveSessions
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
        <NewChatMenu agents={agents} runners={runners} onError={setError} />
      </div>

      {/* `parked.length` opens the toolbar too: with one session, and that one
          held back, the sort row would never render and its own reveal toggle
          would be unreachable — a chat you cannot get back to. */}
      {(sessions.length > 1 || showArchived || parked.length > 0 || showOffline || mineOnly) && (
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
            <FilterToggle checked={mineOnly} onChange={toggleMineOnly} testId="toggle-mine-only">
              Started by me
            </FilterToggle>
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
        mineOnly && scoped.length === 0 && sessions.length > 0 ? (
          <div className="flex flex-col items-center justify-center gap-1 py-12 text-center">
            <div className="text-sm text-foreground">None of these chats were started by you</div>
            <button type="button" onClick={toggleMineOnly} className="text-xs text-primary hover:underline">
              Show all {sessions.length}
            </button>
          </div>
        ) : parked.length > 0 ? (
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
              {agents.length === 0 && repos.length === 0 ? (
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
            const display = sessionDisplay(s)
            const parked = display.tone === 'held'
            const intent = closeIntent(s)
            const closeFailed = closeFailure(s)
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
                    to={scopedPath(s.workspace, `/sessions/${s.id}`)}
                    data-testid={parked ? `session-parked-${s.id}` : undefined}
                    className={`flex min-w-0 flex-1 items-center justify-between gap-3 px-3 py-2.5 hover:bg-muted${
                      parked || s.id in pendingClose ? ' opacity-60' : ''
                    }`}
                  >
                    <div className="min-w-0">
                      <div
                        className="truncate text-sm font-medium text-foreground"
                        title={s.sensitive ? undefined : s.title?.trim() || undefined}
                      >
                        <ListTitle s={s} fallback={sessionDisplayTitle(s.title) || 'Untitled chat'} />
                      </div>
                      <div className="flex min-w-0 items-center gap-1.5 truncate text-xs text-muted-foreground">
                        <WorkspaceBadge workspace={s.workspace} />
                        {label}
                        {s.origin === 'runner' ? ' · discovered' : ''}
                        {s.status !== 'active' ? ` · ${s.status}` : ''}
                      </div>
                    </div>
                    <div className="flex shrink-0 flex-col items-end gap-0.5 text-xs">
                      {/* One status in the shared precedence (sessionDisplay):
                          a parked runner first — it says why the row is dimmed —
                          then a question waiting on you, then running, then idle. */}
                      <span
                        data-testid={`session-status-${s.id}`}
                        data-tone={display.tone}
                        className={clsx(
                          'flex items-center gap-1',
                          display.tone === 'asking' && 'font-medium text-warning',
                          display.tone === 'running' && 'font-medium text-success',
                          (display.tone === 'held' || display.tone === 'idle') && 'text-muted-foreground',
                        )}
                      >
                        <StateDot tone={display.tone} />
                        {display.label}
                      </span>
                      {/* The age used to REPLACE the status, so one slot held two
                          different kinds of fact — five rows read "running" and
                          the sixth read "6h ago", and the column could not be
                          scanned without reading each value to learn which
                          question it was answering. Status always; age alongside. */}
                      <span className="flex items-center gap-1 text-muted-foreground">
                        <TurnModeBadge mode={s.turn_mode} testId={`session-mode-${s.id}`} />
                        {relativeAge(s.last_activity_at, now, { seconds: false })}
                      </span>
                      {display.runner && (
                        <span
                          className="text-muted-foreground"
                          title={display.runnerRequested ? `Starts on ${s.requested_runner_name} with its first message` : undefined}
                        >
                          {display.runner}
                          {s.runner_name && s.runner_location ? ` · ${s.runner_location}` : ''}
                        </span>
                      )}
                    </div>
                  </Link>
                  <TransferSessionMenu session={s} onResult={onTransferred} />
                  <button
                    type="button"
                    data-testid={`close-session-${s.id}`}
                    aria-label={`Close ${listTitle(s, s.title?.trim() || 'Untitled chat')}`}
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
                {closeFailed && !(s.id in pendingClose) && (
                  <CloseFailedNote
                    reason={closeFailed}
                    onRetry={() => void onClose(s)}
                    busy={closingId === s.id}
                    testId={`session-close-failed-${s.id}`}
                    className="px-3 pb-2"
                  />
                )}
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

/** What came with a move, in one clause: the source is packing its work, or it
 * could not and the session starts with NO SNAPSHOT (apps/canopy_sessions/handoff.py). */
export function handoffNote(result: TransferResult | null): string {
  const h = result?.handoff
  if (!h) return ''
  if (h.capture_status === 'pending') return ` ${h.source_runner} is packing its work; the session continues once it has.`
  if (h.capture_status === 'captured') return ' Its work came with it.'
  return ` NO SNAPSHOT — ${h.capture_note || 'the old runner did not hand its work over'}.`
}
