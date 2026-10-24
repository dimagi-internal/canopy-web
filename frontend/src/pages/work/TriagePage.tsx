// /w/:workspace/triage — what waits for a decision: tasks agents suggested,
// ideas people added and proposed projects, oldest first, one dense row each
// with its reasons and backing. Decided with one key on the focused row (J/K):
// A accept, S start now, B backlog, D decline, H snooze, M duplicate; Space or
// Enter peeks the task. A decided row leaves at once and comes back, with the
// server's reason, if the server refuses. A proposed project is decided on its
// own page (its decision card is there), so its row only opens it.
import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ChevronDown, ChevronRight, ThumbsDown, ThumbsUp } from 'lucide-react'
import { StatusBadge } from 'canopy-ui/ui'
import { cn, type StatusTone } from 'canopy-ui/lib'

import { actOn, listTriage, type TaskActionName, type TriageEntry } from '@/api/work'
import { PriorityIcon } from '@/components/work/bits'
import { isPlainKey, isTypingTarget } from '@/components/work/keys'
import { displayAgent, displayPerson } from '@/components/work/meta'
import { TaskPeek } from '@/components/work/TaskPeek'
import { useDirectory } from '@/components/work/useDirectory'
import { usePeek } from '@/components/work/usePeek'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'
import { TASK_RESOURCE, useResource } from '@/widget/useResource'

/** How long a burst of task changes waits before Triage re-reads. */
const REFRESH_DEBOUNCE_MS = 600

type Decision = Extract<TaskActionName, 'accept' | 'approve' | 'backlog' | 'decline' | 'snooze' | 'duplicate'>

const DECISION_KEYS: Record<string, Decision> = {
  a: 'accept',
  s: 'approve',
  b: 'backlog',
  d: 'decline',
  h: 'snooze',
  m: 'duplicate',
}

const KIND: Record<string, { label: string; tone: StatusTone }> = {
  task: { label: 'Task', tone: 'warning' },
  idea: { label: 'Idea', tone: 'special' },
  project: { label: 'Project', tone: 'info' },
}

const CONFIDENCE_TONE: Record<string, string> = {
  high: 'text-success',
  medium: 'text-warning',
  low: 'text-muted-foreground',
}

/** Who suggested it: an agent's name, or a person's. */
function proposer(by: string): string {
  if (!by) return '—'
  return by.includes('@') ? displayPerson(by) : displayAgent(by)
}

/** What "Start now" does, in words: the proposing agent starts on it. */
export function startsLine(entry: Pick<TriageEntry, 'proposed_by'>): string {
  const by = entry.proposed_by
  return by && !by.includes('@') ? `starts ${displayAgent(by)} now` : 'starts it now (In progress)'
}

const byAge = (a: TriageEntry, b: TriageEntry) => Date.parse(a.created_at) - Date.parse(b.created_at)

export function TriagePage(): JSX.Element {
  const { workspace = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const project = params.get('project') ?? ''
  const navigate = useNavigate()
  const { peek, open: openPeek } = usePeek()
  const directory = useDirectory(workspace)

  const [rows, setRows] = useState<TriageEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [refusal, setRefusal] = useState<string | null>(null)
  const [focused, setFocused] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set())

  const rowsRef = useRef(rows)
  rowsRef.current = rows
  const seq = useRef(0)
  const reload = useCallback(() => {
    const mine = ++seq.current
    listTriage({ project }, workspace)
      .then((r) => {
        if (mine !== seq.current) return
        setRows([...r].sort(byAge))
        setError(null)
      })
      .catch((e: unknown) => mine === seq.current && setError(e instanceof Error ? e.message : 'Could not load Triage'))
  }, [project, workspace])
  useEffect(() => reload(), [reload])

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useResource(TASK_RESOURCE, () => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(reload, REFRESH_DEBOUNCE_MS)
  })
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])

  // The focused row: kept on the same item while the list changes, else the first.
  const list = useMemo(() => rows ?? [], [rows])
  const focusedRef = list.some((r) => r.ref === focused) ? focused : (list[0]?.ref ?? null)

  const decide = useCallback(
    async (entry: TriageEntry, action: Decision) => {
      if (entry.kind === 'project') return
      let comment = ''
      if (action === 'duplicate') {
        const other = window.prompt(`${entry.ref} duplicates which task? Its key, e.g. ${entry.ref.split('-')[0]}-12`)?.trim()
        if (!other) return
        comment = other.toUpperCase()
      }
      setRefusal(null)
      // Optimistic: the row leaves now, and focus moves to the row after it.
      const cur = rowsRef.current ?? []
      const i = cur.findIndex((r) => r.ref === entry.ref)
      const next = cur.filter((r) => r.ref !== entry.ref)
      setFocused(next[Math.min(i, next.length - 1)]?.ref ?? null)
      setRows((rs) => (rs ? rs.filter((r) => r.ref !== entry.ref) : rs))
      try {
        await actOn(entry.ref, action, comment)
      } catch (e) {
        setRows((cur) => (cur && !cur.some((r) => r.ref === entry.ref) ? [...cur, entry].sort(byAge) : cur))
        setFocused(entry.ref)
        setRefusal(`${entry.ref}: ${e instanceof Error ? e.message : 'the server refused'}`)
      }
    },
    [],
  )

  const openEntry = useCallback(
    (entry: TriageEntry) => {
      if (entry.kind === 'project') navigate(entry.url)
      else openPeek(entry.ref)
    },
    [navigate, openPeek],
  )

  // The key handler is registered once and reads what is on screen now, so a key
  // pressed right after the list loads is never handled against the empty list of
  // a render whose effect has not run yet.
  const live = useRef({ list, focusedRef, decide, openEntry })
  live.current = { list, focusedRef, decide, openEntry }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!isPlainKey(e) || isTypingTarget(e.target) || e.defaultPrevented) return
      // A dialog (the palette, the help) owns the keyboard while it is open.
      if (document.querySelector('[aria-modal="true"]')) return
      const { list, focusedRef, decide, openEntry } = live.current
      const i = list.findIndex((r) => r.ref === focusedRef)
      const entry = list[i]
      const key = e.key.length === 1 ? e.key.toLowerCase() : e.key
      if (key === 'j' || key === 'ArrowDown') {
        e.preventDefault()
        if (list.length) setFocused(list[Math.min(i + 1, list.length - 1)].ref)
      } else if (key === 'k' || key === 'ArrowUp') {
        e.preventDefault()
        if (list.length) setFocused(list[Math.max(i - 1, 0)].ref)
      } else if ((key === ' ' || key === 'Enter') && entry) {
        // A focused button's own Enter/Space is a click on it; leave it be.
        if (e.target instanceof HTMLButtonElement || e.target instanceof HTMLAnchorElement) return
        e.preventDefault()
        openEntry(entry)
      } else if (entry && DECISION_KEYS[key] && !e.shiftKey) {
        e.preventDefault()
        void decide(entry, DECISION_KEYS[key])
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const setProject = (address: string) =>
    setParams(
      (p) => {
        const next = new URLSearchParams(p)
        if (address) next.set('project', address)
        else next.delete('project')
        return next
      },
      { replace: true },
    )

  const toggle = (ref: string) =>
    setExpanded((cur) => {
      const next = new Set(cur)
      if (next.has(ref)) next.delete(ref)
      else next.add(ref)
      return next
    })

  return (
    <div className={cn('min-w-0', peek && 'xl:pr-[44rem]')}>
      <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-foreground">Triage</h1>
          <p className="mt-1 text-[13px] text-muted-foreground">
            What waits for a decision, oldest first. <Kbd>J</Kbd>/<Kbd>K</Kbd> move,{' '}
            <Kbd>A</Kbd> accept, <Kbd>S</Kbd> start now, <Kbd>B</Kbd> backlog, <Kbd>D</Kbd> decline,{' '}
            <Kbd>H</Kbd> snooze a week, <Kbd>M</Kbd> duplicate, <Kbd>Space</Kbd> open.
          </p>
        </div>
        <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
          Project
          <select
            value={project}
            onChange={(e) => setProject(e.target.value)}
            aria-label="Filter by project"
            className="max-w-56 rounded border border-input bg-input px-2 py-1 text-[12px] text-foreground"
          >
            <option value="">All projects</option>
            {directory.projects.map((p) => (
              <option key={p.id} value={p.address}>
                {p.name}
              </option>
            ))}
            {project && !directory.projects.some((p) => p.address === project) && <option value={project}>{project}</option>}
          </select>
        </label>
      </div>

      {refusal && (
        <p role="alert" className="mb-2 rounded border border-destructive/30 bg-destructive/10 px-3 py-1.5 text-[12.5px] text-destructive">
          {refusal}
        </p>
      )}
      {error && <p className="mb-2 text-[13px] text-destructive">{error}</p>}
      {rows === null && !error && <p className="text-[13px] text-muted-foreground">Loading…</p>}

      {rows !== null && rows.length === 0 && (
        <div className="rounded-lg border border-border bg-card px-4 py-6 text-[13px] text-muted-foreground" data-testid="triage-empty">
          Nothing is waiting for a decision{project ? ' in this project' : ''}. New suggestions, ideas and proposed projects land here.
        </div>
      )}

      {rows !== null && rows.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border bg-card" data-testid="triage-table">
          <table className="w-full table-fixed border-collapse text-[13px]">
            <colgroup>
              <col className="w-[4.5rem]" />
              <col className="w-7" />
              <col className="w-[5.5rem]" />
              <col />
              <col className="hidden w-28 md:table-column" />
              <col className="hidden w-40 lg:table-column" />
              <col className="hidden w-16 sm:table-column" />
              <col className="hidden w-16 md:table-column" />
              <col className="w-14" />
              <col className="w-[17rem]" />
            </colgroup>
            <thead>
              <tr className="border-b border-border text-left text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                <th className="px-2 py-1.5 font-medium">Kind</th>
                <th className="sr-only">Priority</th>
                <th className="px-1 py-1.5 font-medium">Key</th>
                <th className="py-1.5 font-medium">What and why</th>
                <th className="hidden py-1.5 font-medium md:table-cell">Proposed by</th>
                <th className="hidden py-1.5 font-medium lg:table-cell">Project</th>
                <th className="hidden py-1.5 font-medium sm:table-cell" title="Backing: for / passes">Backing</th>
                <th className="hidden py-1.5 font-medium md:table-cell">Confidence</th>
                <th className="py-1.5 pr-2 text-right font-medium">Age</th>
                <th className="py-1.5 pr-2 text-right font-medium">Decide</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((entry) => (
                <TriageRow
                  key={`${entry.kind}:${entry.ref}`}
                  entry={entry}
                  workspace={workspace}
                  focused={entry.ref === focusedRef}
                  expanded={expanded.has(entry.ref)}
                  onToggle={() => toggle(entry.ref)}
                  onFocus={() => setFocused(entry.ref)}
                  onOpen={() => openEntry(entry)}
                  onDecide={(a) => void decide(entry, a)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <TaskPeek />
    </div>
  )
}

function Kbd({ children }: { children: string }): JSX.Element {
  return <kbd className="rounded border border-border bg-muted px-1 font-mono text-[11px] text-foreground-secondary">{children}</kbd>
}

const ACTIONS: { action: Decision; label: string; key: string; title: (e: TriageEntry) => string; primary?: boolean }[] = [
  { action: 'accept', label: 'Accept', key: 'A', title: () => 'Accept it into Todo; nothing starts' },
  { action: 'approve', label: 'Start', key: 'S', title: (e) => `Start now: ${startsLine(e)}`, primary: true },
  { action: 'backlog', label: 'Backlog', key: 'B', title: () => 'Accepted, not now' },
  { action: 'decline', label: 'Decline', key: 'D', title: () => 'Decline it' },
  { action: 'snooze', label: 'Snooze', key: 'H', title: () => 'Out of Triage for a week, or until something happens on it' },
  { action: 'duplicate', label: 'Dup', key: 'M', title: () => 'Mark it a duplicate of another task (asks for its key)' },
]

function TriageRow({
  entry,
  workspace,
  focused,
  expanded,
  onToggle,
  onFocus,
  onOpen,
  onDecide,
}: {
  entry: TriageEntry
  workspace: string
  focused: boolean
  expanded: boolean
  onToggle: () => void
  onFocus: () => void
  onOpen: () => void
  onDecide: (a: Decision) => void
}): JSX.Element {
  const kind = KIND[entry.kind] ?? KIND.task
  const isProject = entry.kind === 'project'
  const projectHandle = entry.project_address?.split('/')
  const quote = entry.human_why?.[0]
  return (
    <tr
      data-triage={entry.ref}
      aria-current={focused ? 'true' : undefined}
      onClick={onFocus}
      className={cn(
        'group border-b border-border align-top last:border-0 hover:bg-muted/50',
        focused && 'bg-primary/5 outline outline-1 -outline-offset-1 outline-primary/60',
      )}
    >
      <td className="px-2 py-1.5">
        <StatusBadge tone={kind.tone}>{kind.label}</StatusBadge>
      </td>
      <td className="px-1 py-1.5">{!isProject && <PriorityIcon priority={entry.priority ?? 0} />}</td>
      <td className="truncate px-1 py-1.5 font-mono text-[11.5px] text-muted-foreground" title={entry.ref}>
        {isProject ? '—' : entry.ref}
      </td>
      <td className="min-w-0 py-1.5 pr-2">
        <div className="flex min-w-0 items-center gap-1.5">
          {isProject ? (
            <Link to={entry.url} className="truncate font-medium text-foreground hover:text-primary">
              {entry.title}
            </Link>
          ) : (
            <button type="button" onClick={onOpen} className="truncate text-left font-medium text-foreground hover:text-primary">
              {entry.title}
            </button>
          )}
          {entry.ask_kind === 'question' && (
            <span className="shrink-0 rounded border border-warning/30 bg-warning/10 px-1 text-[10.5px] font-medium text-warning">Question</span>
          )}
        </div>
        {(entry.why || quote) && (
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={expanded}
            className="mt-0.5 flex w-full min-w-0 items-start gap-1 text-left text-[12px] text-foreground-secondary hover:text-foreground"
          >
            {expanded ? (
              <ChevronDown aria-hidden="true" className="mt-0.5 size-3 shrink-0" />
            ) : (
              <ChevronRight aria-hidden="true" className="mt-0.5 size-3 shrink-0" />
            )}
            <span className={cn('min-w-0', expanded ? 'whitespace-pre-wrap' : 'truncate')}>{entry.why || `“${quote?.quote}”`}</span>
          </button>
        )}
        {expanded && quote && entry.why && (
          <p className="mt-1 border-l-2 border-border pl-2 text-[12px] italic text-foreground-secondary">
            “{quote.quote}”
          </p>
        )}
        {isProject && <p className="mt-0.5 text-[11.5px] text-muted-foreground">Decide it on its page.</p>}
      </td>
      <td className="hidden truncate py-1.5 pr-2 text-[12px] text-foreground-secondary md:table-cell" title={entry.proposed_by}>
        {proposer(entry.proposed_by)}
      </td>
      <td className="hidden truncate py-1.5 pr-2 text-[12px] lg:table-cell">
        {entry.project_address && !isProject && projectHandle && projectHandle.length === 2 ? (
          <Link to={scopedPath(projectHandle[0] || workspace, `/projects/${projectHandle[1]}`)} className="text-foreground-secondary hover:text-primary">
            {entry.project_name || entry.project_address}
          </Link>
        ) : (
          <span className="text-foreground-subtle">—</span>
        )}
      </td>
      <td className="hidden whitespace-nowrap py-1.5 pr-2 text-[12px] text-muted-foreground sm:table-cell" title={`${entry.backers} for, ${entry.passes} passed`}>
        <span className="inline-flex items-center gap-0.5">
          <ThumbsUp aria-hidden="true" className="size-3" />
          {entry.backers}
        </span>{' '}
        <span className="inline-flex items-center gap-0.5">
          <ThumbsDown aria-hidden="true" className="size-3" />
          {entry.passes}
        </span>
      </td>
      <td className={cn('hidden py-1.5 pr-2 text-[12px] capitalize md:table-cell', CONFIDENCE_TONE[entry.confidence] ?? 'text-muted-foreground')}>
        {entry.confidence || '—'}
      </td>
      <td className="whitespace-nowrap py-1.5 pr-2 text-right text-[11.5px] text-muted-foreground" title={entry.created_at}>
        {relativeAge(entry.created_at, new Date(), { seconds: false })}
      </td>
      <td className="py-1 pr-2 text-right">
        {isProject ? (
          <Link to={entry.url} className="rounded border border-border px-1.5 py-0.5 text-[12px] text-foreground-secondary hover:bg-muted">
            Open project
          </Link>
        ) : (
          <div className="inline-flex flex-wrap justify-end gap-0.5">
            {ACTIONS.map((a) => (
              <button
                key={a.action}
                type="button"
                aria-label={`${a.label} ${entry.ref}`}
                title={`${a.title(entry)} (${a.key})`}
                onClick={(e) => {
                  e.stopPropagation()
                  onDecide(a.action)
                }}
                className={cn(
                  'rounded px-1.5 py-0.5 text-[11.5px]',
                  a.primary
                    ? 'bg-primary font-medium text-primary-foreground hover:bg-primary/90'
                    : 'border border-border text-foreground-secondary hover:bg-muted',
                )}
              >
                {a.label}
              </button>
            ))}
          </div>
        )}
        {focused && !isProject && <p className="mt-0.5 text-[11px] text-muted-foreground">S {startsLine(entry)}</p>}
      </td>
    </tr>
  )
}
