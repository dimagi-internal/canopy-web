import { useCallback, useEffect, useMemo, useState, type JSX, type KeyboardEvent } from 'react'
import { Link } from 'react-router-dom'
import { Button, Textarea } from 'canopy-ui/ui'
import { closeSession, listSessions, sendMessage, type ChatSession } from '@/api/chat'
import type { AgentOut } from '@/api/agents'
import { Markdown } from '@/components/Markdown'
import { relativeTime } from '@/components/activity/turnLog'
import { CLOSE_POLL_MS, closeIntent, closeResultMessage, settleClosing } from '@/components/chat/closeAction'
import { sessionDisplayTitle } from '@/components/chat/sessionDisplayTitle'
import { NewChatMenu } from '@/components/chat/NewChatMenu'
import { CHIPS_AT, COMPACT_ABOVE, feedSessions, feedSources, ranOnItsOwn, sourceKey } from './feedRules'

const POLL_MS = 20_000
// Per-viewer and best-effort: storage can be missing or throw (private window).
const SHOW_AUTO_KEY = 'canopy.supervisor.feed.showAuto'

function readShowAuto(): boolean {
  try {
    return window.localStorage.getItem(SHOW_AUTO_KEY) === '1'
  } catch {
    return false
  }
}

/**
 * The supervisor's main screen: every session that finished a turn (or is
 * blocked on a dialog) and is waiting for your next prompt — what the agent
 * said, rendered, with a reply box right under it. Answer in place, open the
 * chat for the full transcript, or Close it — the same close as the chat page
 * and the session list (ends its emdash task).
 */
export function SessionFeed({ agents }: { agents: AgentOut[] | null }): JSX.Element {
  const [sessions, setSessions] = useState<ChatSession[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Sent or archived from this screen: hidden at once rather than waiting out
  // the poll, which would otherwise show a card you just dealt with for ~20s.
  // Keyed to the activity stamp it was handled at, not just the id: a runner
  // session can claim your reply, run, and finish its NEXT turn between two
  // polls, and that new reply must not stay hidden behind the old dismissal.
  const [handled, setHandled] = useState<Map<string, string>>(() => new Map())
  // The agent/project chip filter (null = All).
  const [source, setSource] = useState<string | null>(null)
  // Closes relayed to a runner and not yet confirmed (id -> when), and the ones
  // the runner never confirmed. Same contract as the Sessions list's ×: the card
  // stays, marked "Closing…", until the runner has deleted the emdash task and
  // its report retires the session — hiding it at once would be a lie whenever
  // the delete failed.
  const [closing, setClosing] = useState<Record<string, number>>({})
  const [stuck, setStuck] = useState<Set<string>>(() => new Set())
  // Sessions an agent drove on its own (auto mode) are held back unless shown.
  const [showAuto, setShowAuto] = useState(readShowAuto)
  const [newChatError, setNewChatError] = useState<string | null>(null)
  const toggleAuto = useCallback(() => {
    setShowAuto((v) => {
      try {
        window.localStorage.setItem(SHOW_AUTO_KEY, v ? '0' : '1')
      } catch { /* the toggle still works for this visit */ }
      return !v
    })
  }, [])

  const reload = useCallback(() => {
    listSessions('active', { reply: true })
      .then((rows) => {
        setSessions(rows)
        setError(null)
        setClosing((prev) => {
          if (Object.keys(prev).length === 0) return prev
          const { pending, stuck: late } = settleClosing(prev, rows, Date.now())
          if (late.length > 0) setStuck((s) => new Set([...s, ...late]))
          return pending
        })
        setHandled((prev) => {
          if (prev.size === 0) return prev
          const stamp = new Map(rows.map((s) => [s.id, s.last_activity_at] as const))
          const next = new Map([...prev].filter(([id, at]) => stamp.get(id) === at))
          return next.size === prev.size ? prev : next
        })
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : 'Failed to load'))
  }, [])

  useEffect(() => {
    reload()
    const id = window.setInterval(reload, POLL_MS)
    const onFocus = () => reload()
    window.addEventListener('focus', onFocus)
    return () => {
      window.clearInterval(id)
      window.removeEventListener('focus', onFocus)
    }
  }, [reload])

  // Watch relayed closes through quickly rather than leaving them to the 20s poll.
  const awaitingClose = Object.keys(closing).length > 0
  useEffect(() => {
    if (!awaitingClose) return
    const id = window.setInterval(reload, CLOSE_POLL_MS)
    return () => window.clearInterval(id)
  }, [awaitingClose, reload])

  const markClosing = useCallback((s: ChatSession) => {
    setStuck((prev) => {
      if (!prev.has(s.id)) return prev
      const next = new Set(prev)
      next.delete(s.id)
      return next
    })
    setClosing((prev) => ({ ...prev, [s.id]: Date.now() }))
  }, [])

  const agentName = useMemo(() => {
    const by = new Map((agents ?? []).map((a) => [a.slug, a.name]))
    return (slug: string | null) => (slug ? by.get(slug) ?? slug : null)
  }, [agents])

  const markHandled = useCallback((s: ChatSession) => {
    setHandled((prev) => new Map(prev).set(s.id, s.last_activity_at))
  }, [])

  if (error && sessions === null) {
    return (
      <p className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-[13px] text-destructive">
        {error}
      </p>
    )
  }
  if (sessions === null) {
    // Placeholder cards, pulsing: on a slow load a line of grey text read as
    // nothing happening.
    return (
      <div className="flex flex-col gap-3" aria-busy="true" aria-label="Loading sessions" data-testid="feed-loading">
        {[0, 1, 2].map((i) => (
          <div key={i} className="animate-pulse rounded-lg border border-border bg-card p-3">
            <div className="h-4 w-1/3 rounded bg-muted" />
            <div className="mt-2 h-3 w-1/4 rounded bg-muted" />
            <div className="mt-4 h-3 w-full rounded bg-muted" />
            <div className="mt-2 h-3 w-5/6 rounded bg-muted" />
            <div className="mt-4 h-14 w-full rounded bg-muted" />
          </div>
        ))}
      </div>
    )
  }

  const { feed, parked } = feedSessions(sessions)
  const unhandled = feed.filter((s) => handled.get(s.id) !== s.last_activity_at)
  const autoCount = unhandled.filter(ranOnItsOwn).length
  const pending = showAuto ? unhandled : unhandled.filter((s) => !ranOnItsOwn(s))
  const sources = feedSources(pending)
  const showChips = pending.length >= CHIPS_AT && sources.length > 1
  // A filter whose source has emptied out falls back to All rather than
  // stranding you on a blank feed.
  const activeSource = showChips && sources.some((x) => x.key === source) ? source : null
  const visible = activeSource ? pending.filter((s) => sourceKey(s) === activeSource) : pending
  const compact = visible.length > COMPACT_ABOVE
  // Name the workspace only when the feed spans several — one person's feed
  // crosses every workspace they are in, and then "which one" matters.
  const multiWorkspace = new Set(pending.map((s) => s.workspace)).size > 1
  const now = new Date()
  const sourceLabel = (s: ChatSession) => agentName(s.agent_slug) ?? (s.project || 'no agent')

  return (
    <div className="flex flex-col gap-3" data-testid="session-feed">
      <div className="flex items-center justify-between gap-2">
        {autoCount > 0 || showAuto ? (
          <label
            className={`inline-flex min-h-9 cursor-pointer items-center gap-1.5 text-[12px] sm:min-h-0 ${
              showAuto ? 'text-foreground' : 'text-muted-foreground hover:text-foreground-secondary'
            }`}
            title="Sessions an agent ran on its own, in auto mode — from a schedule, an email, Slack or a dispatch"
          >
            <input
              type="checkbox"
              checked={showAuto}
              onChange={toggleAuto}
              data-testid="feed-show-auto"
              className="h-3.5 w-3.5 accent-primary"
            />
            Show auto sessions{autoCount > 0 ? ` (${autoCount})` : ''}
          </label>
        ) : (
          <span />
        )}
        <NewChatMenu agents={agents ?? []} onError={setNewChatError} />
      </div>
      {newChatError && <p className="text-[12px] text-destructive">{newChatError}</p>}
      {showChips && (
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter by agent" data-testid="feed-chips">
          {[{ key: null, label: 'All', count: pending.length }, ...sources.map((x) => ({
            key: x.key, label: sourceLabel(x.sample), count: x.count,
          }))].map((c) => (
            <button
              key={c.key ?? 'all'}
              type="button"
              aria-pressed={activeSource === c.key}
              onClick={() => setSource(c.key)}
              className={`min-h-9 rounded-full border px-3 text-[12px] sm:min-h-0 sm:py-1 ${
                activeSource === c.key
                  ? 'border-primary/40 bg-primary/10 font-medium text-primary'
                  : 'border-border text-muted-foreground hover:bg-muted hover:text-foreground'
              }`}
            >
              {c.label} <span className="opacity-70">{c.count}</span>
            </button>
          ))}
        </div>
      )}
      {visible.length === 0 ? (
        <div className="rounded-lg border border-border bg-card p-4 text-center" data-testid="feed-empty">
          <p className="text-sm font-medium text-foreground">You're all caught up</p>
          <p className="mt-1 text-[12px] text-muted-foreground">
            When a session finishes a turn and needs your next prompt, it shows up here.
            {!showAuto && autoCount > 0 &&
              ` ${autoCount} session${autoCount === 1 ? '' : 's'} an agent ran on its own ${autoCount === 1 ? 'is' : 'are'} hidden.`}
          </p>
        </div>
      ) : (
        visible.map((s) => (
          <FeedCard
            key={s.id}
            session={s}
            source={sourceLabel(s)}
            sourceKind={s.agent_slug ? 'agent' : 'project'}
            workspace={multiWorkspace ? s.workspace : null}
            age={relativeTime(s.last_activity_at, now)}
            compact={compact}
            closing={s.id in closing}
            stuck={stuck.has(s.id)}
            onHandled={markHandled}
            onClosing={markClosing}
            onClosed={reload}
          />
        ))
      )}
      {parked > 0 && (
        <Link
          to="/supervisor?tab=sessions"
          className="self-start text-[12px] text-muted-foreground hover:text-foreground"
          data-testid="feed-parked"
        >
          + {parked} more waiting on a paused or offline runner →
        </Link>
      )}
    </div>
  )
}

function FeedCard({
  session: s,
  source,
  sourceKind,
  workspace,
  age,
  compact,
  closing,
  stuck,
  onHandled,
  onClosing,
  onClosed,
}: {
  session: ChatSession
  /** The agent's name, or for an agentless chat its project. */
  source: string
  sourceKind: 'agent' | 'project'
  /** Named only when the feed spans several workspaces. */
  workspace: string | null
  age: string
  compact: boolean
  /** A close was relayed to the runner and it has not confirmed yet. */
  closing: boolean
  /** The runner never confirmed a close (CLOSE_CONFIRM_TIMEOUT_MS). */
  stuck: boolean
  onHandled: (s: ChatSession) => void
  onClosing: (s: ChatSession) => void
  onClosed: () => void
}): JSX.Element {
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState<'send' | 'done' | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)
  const chatHref = `/w/${s.workspace}/chat/${s.id}`
  const reply = (s.last_reply ?? '').trim()
  // Long replies are clamped so one essay does not push every other card off
  // the screen; "Show more" opens it in place. In a long feed (compact) every
  // card starts as a short preview, so the list stays scannable.
  const long = compact
    ? reply.length > 240 || reply.split('\n').length > 4
    : reply.length > 700 || reply.split('\n').length > 12

  const send = async () => {
    const text = draft.trim()
    if (!text || busy) return
    setBusy('send')
    setErr(null)
    try {
      await sendMessage(s.id, text, crypto.randomUUID())
      onHandled(s)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Send failed')
      setBusy(null)
    }
  }

  // A real close, not an archive. Archiving only flips the row's status, and a
  // runner session's emdash task is still open — the runner's next report
  // (~10s) un-archives anything it reports open (harness/services.py), so the
  // card came straight back. /close deletes the task; the report then retires it.
  const intent = closeIntent(s)
  const done = async () => {
    if (busy || closing) return
    if (intent.kind === 'blocked') {
      setErr(intent.why)
      return
    }
    if (intent.confirm && !window.confirm(`${s.title?.trim() || 'This chat'} is still working. Close it anyway?`)) {
      return
    }
    setBusy('done')
    setErr(null)
    try {
      const result = await closeSession(s.id)
      const message = closeResultMessage(result, s)
      if (message) setErr(message)
      // Relayed to the runner: the card stays, marked, until its report has
      // retired the session — hiding it on faith would hide a failed delete.
      else if (result.closing) onClosing(s)
      else onClosed()   // closed server-side (unbound) — it leaves the list now
      setBusy(null)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not close this session')
      setBusy(null)
    }
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      void send()
    }
  }

  return (
    <article
      className="rounded-lg border border-border bg-card"
      data-testid={`feed-card-${s.id}`}
    >
      {/* Which agent (or project) this is matters as much as the title — a
          feed spans the whole fleet — so it leads the card, on one compact
          line with the status, rather than trailing in grey under the title. */}
      <header className="px-3 pt-3">
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-1.5 text-[12px] text-muted-foreground">
            <span
              className={`shrink-0 truncate rounded px-1.5 py-0.5 text-[12px] font-semibold ${
                sourceKind === 'agent' ? 'bg-primary/10 text-primary' : 'bg-muted text-foreground'
              }`}
              data-testid={`feed-source-${s.id}`}
            >
              {source}
            </span>
            <span className="truncate">
              {[workspace, s.runner_name, age].filter(Boolean).join(' · ')}
            </span>
          </div>
          {s.waiting_on_you ? (
            <span className="shrink-0 rounded bg-warning/15 px-1.5 py-0.5 text-[11px] font-medium text-warning">
              needs an answer
            </span>
          ) : (
            <span className="shrink-0 rounded bg-success/15 px-1.5 py-0.5 text-[11px] font-medium text-success">
              turn done
            </span>
          )}
        </div>
        <Link
          to={chatHref}
          className="mt-1 block truncate text-sm font-semibold text-foreground hover:underline"
          title={s.title?.trim() || undefined}
        >
          {sessionDisplayTitle(s.title) || 'Untitled chat'}
        </Link>
      </header>

      <div className="px-3 pt-2">
        {reply ? (
          // The reply itself toggles a long card open AND closed — only the small
          // "Show less" at the very bottom used to close it, which after a long
          // reply is a scroll away. Not when the click lands on a link in the
          // reply, or ends a text selection.
          <div
            className={`${long ? 'cursor-pointer' : ''} ${long && !expanded ? `relative overflow-hidden ${compact ? 'max-h-24' : 'max-h-60'}` : ''}`}
            onClick={(e) => {
              if (!long) return
              if ((e.target as HTMLElement).closest('a')) return
              if (window.getSelection()?.toString()) return
              setExpanded((v) => !v)
            }}
            data-testid={`feed-reply-${s.id}`}
          >
            <Markdown className="text-[13px] leading-relaxed text-foreground-secondary">{reply}</Markdown>
            {long && !expanded && (
              <div className="pointer-events-none absolute inset-x-0 bottom-0 h-12 bg-gradient-to-t from-card to-transparent" />
            )}
          </div>
        ) : (
          <p className="text-[13px] italic text-muted-foreground">No reply text — open the chat to see where it stopped.</p>
        )}
        {long && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="mt-1 text-[12px] text-primary hover:underline"
          >
            {expanded ? 'Show less' : 'Show more'}
          </button>
        )}
        {s.waiting_on_you && (
          <p className="mt-2 rounded-md bg-warning/10 px-2 py-1.5 text-[12px] text-warning">
            The agent is blocked on a question.{' '}
            <Link to={chatHref} className="font-medium underline underline-offset-2">
              Answer it in the chat →
            </Link>
          </p>
        )}
      </div>

      <div className="flex flex-col gap-2 p-3">
        <Textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          rows={2}
          placeholder="Your next prompt…"
          aria-label={`Reply to ${s.title?.trim() || 'this session'}`}
          disabled={busy !== null}
          // The repo's readable-placeholder pair (e2e/mobile-a11y.spec.ts gates
          // 3:1): muted alone reaches only ~2.1 on a dark input.
          className="text-[13px] placeholder:text-muted-foreground dark:placeholder:text-foreground-secondary"
        />
        {err && <p className="text-[12px] text-destructive">{err}</p>}
        {stuck && !closing && (
          <p className="text-[12px] text-destructive" data-testid={`feed-close-stuck-${s.id}`}>
            Still open: {s.runner_name ?? 'its runner'} has not confirmed the close. Try again, or close it in emdash.
          </p>
        )}
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5">
            {/* Bordered, so they read as buttons rather than captions. */}
            <Link
              to={chatHref}
              className="inline-flex min-h-8 items-center rounded-md border border-border bg-background px-2.5 text-[12px] font-medium text-foreground hover:bg-muted"
            >
              Open chat
            </Link>
            <button
              type="button"
              onClick={() => void done()}
              disabled={busy !== null || closing}
              title={
                intent.kind === 'blocked'
                  ? intent.why
                  : 'Close this session — ends its emdash task and removes it from the feed'
              }
              className="inline-flex min-h-8 items-center rounded-md border border-border bg-background px-2.5 text-[12px] font-medium text-foreground hover:bg-muted disabled:opacity-50"
              data-testid={`feed-done-${s.id}`}
            >
              {closing ? 'Closing in emdash…' : busy === 'done' ? 'Closing…' : 'Close'}
            </button>
          </div>
          <Button size="sm" onClick={() => void send()} disabled={!draft.trim() || busy !== null}>
            {busy === 'send' ? 'Sending…' : 'Send'}
          </Button>
        </div>
      </div>
    </article>
  )
}
