import { useCallback, useEffect, useMemo, useState, type JSX, type KeyboardEvent } from 'react'
import { Link } from 'react-router-dom'
import { Button, Textarea } from 'canopy-ui/ui'
import { archiveSession, listSessions, sendMessage, type ChatSession } from '@/api/chat'
import type { AgentOut } from '@/api/agents'
import { Markdown } from '@/components/Markdown'
import { relativeTime } from '@/components/activity/turnLog'
import { sessionDisplayTitle } from '@/components/chat/sessionDisplayTitle'
import { sessionTargetLabel } from '@/components/chat/sessionTargetLabel'
import { CHIPS_AT, COMPACT_ABOVE, feedSessions, feedSources, sourceKey } from './feedRules'

const POLL_MS = 20_000

/**
 * The supervisor's main screen: every session that finished a turn (or is
 * blocked on a dialog) and is waiting for your next prompt — what the agent
 * said, rendered, with a reply box right under it. Answer in place, open the
 * chat for the full transcript, or mark it Done (archive; reversible from
 * Sessions → Show archived).
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

  const reload = useCallback(() => {
    listSessions('active', { reply: true })
      .then((rows) => {
        setSessions(rows)
        setError(null)
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
    return <div className="py-6 text-sm text-muted-foreground">Loading…</div>
  }

  const { feed, parked } = feedSessions(sessions)
  const pending = feed.filter((s) => handled.get(s.id) !== s.last_activity_at)
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
          </p>
        </div>
      ) : (
        visible.map((s) => (
          <FeedCard
            key={s.id}
            session={s}
            label={
              sessionTargetLabel(agentName(s.agent_slug), s.project ?? '') +
              (multiWorkspace ? ` · ${s.workspace}` : '')
            }
            age={relativeTime(s.last_activity_at, now)}
            compact={compact}
            onHandled={markHandled}
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
  label,
  age,
  compact,
  onHandled,
}: {
  session: ChatSession
  label: string
  age: string
  compact: boolean
  onHandled: (s: ChatSession) => void
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

  const done = async () => {
    if (busy) return
    setBusy('done')
    setErr(null)
    try {
      await archiveSession(s.id)
      onHandled(s)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not archive')
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
      <header className="flex items-start justify-between gap-3 px-3 pt-3">
        <div className="min-w-0">
          <Link
            to={chatHref}
            className="block truncate text-sm font-semibold text-foreground hover:underline"
            title={s.title?.trim() || undefined}
          >
            {sessionDisplayTitle(s.title) || 'Untitled chat'}
          </Link>
          <div className="truncate text-[12px] text-muted-foreground">
            {label}
            {s.runner_name ? ` · ${s.runner_name}` : ''} · {age}
          </div>
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
      </header>

      <div className="px-3 pt-2">
        {reply ? (
          <div className={long && !expanded ? `relative overflow-hidden ${compact ? 'max-h-24' : 'max-h-60'}` : ''}>
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
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1">
            <Link
              to={chatHref}
              className="rounded-md px-2 py-1 text-[12px] text-muted-foreground hover:bg-muted hover:text-foreground"
            >
              Open chat
            </Link>
            <button
              type="button"
              onClick={() => void done()}
              disabled={busy !== null}
              title="Archive this session — it leaves the feed; Sessions → Show archived brings it back"
              className="rounded-md px-2 py-1 text-[12px] text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50"
              data-testid={`feed-done-${s.id}`}
            >
              {busy === 'done' ? 'Archiving…' : 'Done'}
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
