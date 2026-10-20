import { useCallback, useEffect, useMemo, useState, type JSX, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { actOnTask, type ProjectOut } from '@/api/agents'
import { attentionPins, loadAttention, letGo, takeBack, type Attention, type AttentionRow } from '@/api/attention'
import { apiV2, WORKSPACE_HEADER } from '@/api/client.v2'
import { useAuth } from '@/auth/AuthProvider'
import { relativeTime } from '@/components/activity/turnLog'
import { useWorkspace } from '@/workspace/WorkspaceProvider'

// HOME — what is waiting on you, across every workspace you are in.
//
// One list, read from `/api/attention/` (the same list MCP and any client get),
// ranked by what just happened rather than as a queue: an oldest-first queue
// floated whatever had gone stale to the top, where it stayed. Anything nobody
// has touched in a few days drops into "Gone quiet", folded, with one button to
// let all of it go. Letting go is not deciding: the agent is not told and the
// task is unchanged; it comes back by itself when something new happens on it.

/** How many rows "Needs you" shows before "Show more" — the list is meant to be
 *  read in one look, not worked through. */
const SHOWN = 8

const KIND_LABEL: Record<string, string> = {
  task: 'Task',
  session: 'Chat',
  access_request: 'Access',
}

export function HomePage(): JSX.Element {
  const auth = useAuth()
  const { workspaces, loading: wsLoading } = useWorkspace()
  const [data, setData] = useState<Attention | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<string>('')
  const [showAll, setShowAll] = useState(false)
  const [showQuiet, setShowQuiet] = useState(false)
  const [showLetGo, setShowLetGo] = useState(false)
  const [undo, setUndo] = useState<{ keys: string[]; label: string } | null>(null)

  const refresh = useCallback(async () => {
    if (!workspaces.length) return
    try {
      setData(await loadAttention(workspaces))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load')
    }
  }, [workspaces])

  useEffect(() => {
    void refresh()
    const t = window.setInterval(() => void refresh(), 30_000)
    return () => window.clearInterval(t)
  }, [refresh])

  const names = useMemo(() => new Map(workspaces.map((w) => [w.slug, w.display_name])), [workspaces])
  const multi = workspaces.length > 1
  const inFilter = (i: AttentionRow) => !filter || i.workspace === filter
  const now = (data?.now ?? []).filter(inFilter)
  const quiet = (data?.quiet ?? []).filter(inFilter)
  const lettingGo = (data?.let_go ?? []).filter(inFilter)
  const counts = useMemo(() => {
    const c = new Map<string, number>()
    for (const i of data?.now ?? []) c.set(i.workspace, (c.get(i.workspace) ?? 0) + 1)
    return c
  }, [data])

  // Optimistic: the row leaves at once; a failure puts it back on the next read.
  function drop(keys: string[]) {
    setData((d) =>
      d && {
        now: d.now.filter((i) => !keys.includes(i.key)),
        quiet: d.quiet.filter((i) => !keys.includes(i.key)),
        let_go: d.let_go,
      },
    )
  }

  async function onLetGo(items: AttentionRow[], days?: number) {
    const keys = items.map((i) => i.key)
    drop(keys)
    setUndo({
      keys,
      label: items.length === 1 ? `Let go of “${items[0].title}”` : `Let go of ${items.length} items`,
    })
    try {
      await letGo(keys, days)
    } finally {
      void refresh()
    }
  }

  async function onTakeBack(keys: string[]) {
    setUndo(null)
    await Promise.all(keys.map((k) => takeBack(k)))
    void refresh()
  }

  async function onAct(item: AttentionRow, action: 'approve' | 'decline' | 'reply', comment = '') {
    drop([item.key])
    try {
      await actOnTask(item.target.agent, item.target.task, action, comment, item.workspace)
    } finally {
      void refresh()
    }
  }

  const firstName = auth.status === 'authenticated' ? (auth.user.name || auth.user.email).split(/[ @]/)[0] : ''
  const shown = showAll ? now : now.slice(0, SHOWN)

  return (
    <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_18rem]">
      <div className="min-w-0">
        <div className="mb-4">
          <h1 className="text-xl font-semibold text-foreground">Home</h1>
          <p className="mt-1 text-[13px] text-muted-foreground">
            {data == null
              ? 'What is waiting on you, across every workspace you are in.'
              : now.length === 0
                ? `Nothing new needs you${firstName ? `, ${firstName}` : ''}.`
                : `${now.length} ${now.length === 1 ? 'thing needs' : 'things need'} you. Newest first — let go of anything you won't get to.`}
          </p>
        </div>

        {multi && (
          <div className="mb-3 flex flex-wrap gap-1.5 text-[12px]" role="group" aria-label="Workspace">
            {[{ slug: '', label: 'All workspaces' }, ...workspaces.map((w) => ({ slug: w.slug, label: w.display_name }))]
              .filter((w) => !w.slug || counts.get(w.slug) || filter === w.slug)
              .map((w) => (
                <button
                  key={w.slug || 'all'}
                  type="button"
                  onClick={() => setFilter(w.slug)}
                  className={`rounded-full border px-2.5 py-0.5 ${
                    filter === w.slug
                      ? 'border-primary/30 bg-primary/10 font-medium text-primary'
                      : 'border-border text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {w.label}
                  {w.slug && counts.get(w.slug) ? <span className="ml-1 tabular-nums">{counts.get(w.slug)}</span> : null}
                </button>
              ))}
          </div>
        )}

        {undo && (
          <div className="mb-3 flex items-center justify-between gap-3 rounded border border-border bg-card px-3 py-2 text-[13px]">
            <span className="truncate text-foreground-secondary">
              {undo.label}. It comes back when something new happens on it.
            </span>
            <button type="button" onClick={() => void onTakeBack(undo.keys)} className="shrink-0 font-medium text-primary hover:underline">
              Undo
            </button>
          </div>
        )}

        {error && <p className="mb-3 text-sm text-destructive">{error}</p>}
        {(data == null && !error) || wsLoading ? <p className="text-sm text-muted-foreground">Loading…</p> : null}

        <Section title="Needs you" count={now.length}>
          {data && now.length === 0 && (
            <p className="px-3 py-4 text-[13px] text-muted-foreground">
              Nothing has happened that needs you in the last few days.
              {quiet.length > 0 && ' Older things that are still open are under Gone quiet.'}
            </p>
          )}
          {shown.map((item) => (
            <AttentionRow key={item.key} item={item} workspaceName={multi ? names.get(item.workspace) : undefined}
              onAct={onAct} onLetGo={onLetGo} />
          ))}
          {now.length > SHOWN && (
            <button type="button" onClick={() => setShowAll((v) => !v)}
              className="w-full border-t border-border px-3 py-2 text-left text-[12px] text-muted-foreground hover:text-foreground">
              {showAll ? 'Show fewer' : `Show ${now.length - SHOWN} more`}
            </button>
          )}
        </Section>

        {quiet.length > 0 && (
          <Section
            title="Gone quiet"
            count={quiet.length}
            hint="Still open, but nothing has happened on these for 3+ days."
            open={showQuiet}
            onToggle={() => setShowQuiet((v) => !v)}
            action={
              <button type="button" onClick={() => void onLetGo(quiet)}
                className="rounded border border-border px-2 py-0.5 text-[12px] text-foreground-secondary hover:bg-muted">
                Let go of all {quiet.length}
              </button>
            }
          >
            {quiet.map((item) => (
              <AttentionRow key={item.key} item={item} workspaceName={multi ? names.get(item.workspace) : undefined}
                onAct={onAct} onLetGo={onLetGo} />
            ))}
          </Section>
        )}

        {lettingGo.length > 0 && (
          <Section
            title="Let go"
            count={lettingGo.length}
            hint="Set aside by you. Each comes back on its own when something new happens on it."
            open={showLetGo}
            onToggle={() => setShowLetGo((v) => !v)}
          >
            {lettingGo.map((item) => (
              <div key={item.key} className="flex items-center gap-3 border-t border-border px-3 py-2 text-[13px] first:border-t-0">
                <KindBadge kind={item.kind} />
                <Link to={item.url} className="min-w-0 flex-1 truncate text-foreground-secondary hover:text-primary">{item.title}</Link>
                <button type="button" onClick={() => void onTakeBack([item.key])} className="shrink-0 text-[12px] text-primary hover:underline">
                  Bring back
                </button>
              </div>
            ))}
          </Section>
        )}
      </div>

      <aside className="flex flex-col gap-6">
        <YourProjects />
        <div>
          <h2 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Across your workspaces</h2>
          <ul className="flex flex-col gap-1 text-[13px]">
            <li><Link to="/activity" className="text-foreground-secondary hover:text-primary">Turn log — what ran</Link></li>
            <li><Link to="/schedules" className="text-foreground-secondary hover:text-primary">Schedules — what will run</Link></li>
            <li><Link to="/supervisor?tab=runners" className="text-foreground-secondary hover:text-primary">Runners</Link></li>
          </ul>
        </div>
      </aside>
    </div>
  )
}

function Section({ title, count, hint, open = true, onToggle, action, children }: {
  title: string
  count: number
  hint?: string
  open?: boolean
  onToggle?: () => void
  action?: JSX.Element
  children: ReactNode
}): JSX.Element {
  return (
    <section className="mb-6">
      <div className="mb-1.5 flex items-center justify-between gap-3">
        <button type="button" onClick={onToggle} disabled={!onToggle}
          className="flex items-center gap-2 text-left disabled:cursor-default" aria-expanded={open}>
          <h2 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            {onToggle && <span className="mr-1 inline-block w-2">{open ? '▾' : '▸'}</span>}
            {title} <span className="tabular-nums">{count}</span>
          </h2>
          {hint && <span className="hidden text-[12px] text-muted-foreground sm:inline">{hint}</span>}
        </button>
        {action}
      </div>
      {open && <div className="overflow-hidden rounded border border-border bg-background">{children}</div>}
    </section>
  )
}

function KindBadge({ kind }: { kind: string }): JSX.Element {
  return (
    <span className="w-12 shrink-0 rounded bg-muted px-1.5 py-0.5 text-center text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
      {KIND_LABEL[kind] ?? kind}
    </span>
  )
}

function AttentionRow({ item, workspaceName, onAct, onLetGo }: {
  item: AttentionRow
  workspaceName?: string
  onAct: (i: AttentionRow, a: 'approve' | 'decline' | 'reply', comment?: string) => Promise<void>
  onLetGo: (items: AttentionRow[], days?: number) => Promise<void>
}): JSX.Element {
  const [replying, setReplying] = useState(false)
  const [text, setText] = useState('')
  const canTask = item.kind === 'task'
  const meta = [item.agent_slug, item.project, workspaceName].filter(Boolean).join(' · ')

  return (
    <div className="group border-t border-border px-3 py-2.5 first:border-t-0 hover:bg-card/60" data-testid="attention-row">
      {/* Wraps: on a phone the actions drop below the text instead of squeezing it. */}
      <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
        <KindBadge kind={item.kind} />
        <div className="min-w-48 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <Link to={item.url} className="text-[13px] font-medium text-foreground hover:text-primary">{item.title}</Link>
            <span className="text-[12px] text-warning">{item.reason}</span>
          </div>
          {item.body && <p className="mt-0.5 line-clamp-2 text-[12px] text-foreground-secondary">{item.body}</p>}
          <div className="mt-0.5 text-[11px] text-muted-foreground">
            {meta && <span>{meta} · </span>}
            <span title={new Date(item.last_activity_at).toLocaleString()}>{relativeTime(item.last_activity_at, new Date())}</span>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1 pl-15 text-[12px] sm:pl-0">
          {canTask && item.actions.includes('approve') && (
            <button type="button" onClick={() => void onAct(item, 'approve')}
              className="rounded bg-primary px-2 py-0.5 font-medium text-primary-foreground hover:bg-primary/90">Approve</button>
          )}
          {canTask && item.actions.includes('decline') && (
            <button type="button" onClick={() => void onAct(item, 'decline')}
              className="rounded border border-border px-2 py-0.5 text-foreground-secondary hover:bg-muted">Decline</button>
          )}
          {canTask && item.actions.includes('reply') && (
            <button type="button" onClick={() => setReplying((v) => !v)}
              className="rounded border border-border px-2 py-0.5 text-foreground-secondary hover:bg-muted">Reply</button>
          )}
          {!canTask && (
            <Link to={item.url} className="rounded border border-border px-2 py-0.5 text-foreground-secondary hover:bg-muted">Open</Link>
          )}
          <button type="button" onClick={() => void onLetGo([item])} title="Take it off your list without deciding. It comes back when something new happens on it."
            className="rounded px-2 py-0.5 text-muted-foreground hover:bg-muted hover:text-foreground">Let go</button>
        </div>
      </div>
      {replying && (
        <form
          className="mt-2 flex gap-2 sm:pl-15"
          onSubmit={(e) => {
            e.preventDefault()
            if (text.trim()) void onAct(item, 'reply', text.trim())
          }}
        >
          <input autoFocus value={text} onChange={(e) => setText(e.target.value)} placeholder={`Reply to ${item.agent_slug}…`}
            className="min-w-0 flex-1 rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground" />
          <button type="submit" className="rounded bg-primary px-2.5 py-1 text-[12px] font-medium text-primary-foreground">Send</button>
        </form>
      )}
    </div>
  )
}

/** Projects you own, across your workspaces — what you are steering. */
function YourProjects(): JSX.Element | null {
  const auth = useAuth()
  const { workspaces } = useWorkspace()
  const [projects, setProjects] = useState<ProjectOut[] | null>(null)
  const me = auth.status === 'authenticated' ? auth.user.email : ''

  useEffect(() => {
    if (!workspaces.length) return
    let cancelled = false
    Promise.all(
      attentionPins(workspaces).map((ws) =>
        apiV2.GET('/api/projects/', { params: { query: { status: 'active' } }, headers: { [WORKSPACE_HEADER]: ws } })
          .then((r) => (r.data ? (Array.from(r.data) as ProjectOut[]) : [])),
      ),
    ).then((parts) => {
      if (cancelled) return
      const byId = new Map(parts.flat().map((p) => [p.id, p]))
      setProjects([...byId.values()])
    }).catch(() => !cancelled && setProjects([]))
    return () => {
      cancelled = true
    }
  }, [workspaces])

  const mine = (projects ?? [])
    .filter((p) => p.owner_email === me)
    .sort((a, b) => b.waiting_task_count - a.waiting_task_count || Date.parse(b.updated_at) - Date.parse(a.updated_at))
    .slice(0, 8)

  return (
    <div>
      <h2 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Your projects</h2>
      {projects && mine.length === 0 && <p className="text-[12px] text-muted-foreground">No active projects you own.</p>}
      <ul className="flex flex-col divide-y divide-border rounded border border-border">
        {mine.map((p) => (
          <li key={p.id} className="px-2.5 py-1.5">
            <Link to={p.board_url || p.url} className="block truncate text-[13px] text-foreground hover:text-primary">{p.name}</Link>
            <div className="text-[11px] text-muted-foreground">
              {p.agent_slug} · {p.open_task_count} open
              {p.waiting_task_count > 0 && <span className="text-warning"> · {p.waiting_task_count} waiting</span>}
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
