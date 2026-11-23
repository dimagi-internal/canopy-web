import { Fragment, useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Button, StatusBadge } from 'canopy-ui/ui'
import type { StatusTone } from 'canopy-ui/lib'

import { listBrainOutputs, setOutputStatus, type BrainOutput, type BrainOutputStatus } from '@/api/brain'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'
import { LineageStrip, Sightings } from './BrainFindingViz'

// The Findings tab of the brain page: what learning found, written for the person who has to act on it. A finding
// reads top to bottom as what it is (chips), where the fix belongs, the fix, the quoted evidence (each quote linked to
// its session when the viewer reads that session, `source_sessions` from the server), then how sure it is and what it
// did not check, then the status buttons. Under the chips, what it reaches (`LineageStrip`: sessions and people across
// every repeat, opening the evidence) and when it was seen (`Sightings`). A finding that repeats one already done says
// it came back; a dismissed one seen again since says so. A body's fields are free-form per learner: the known ones (`brain-learn`'s
// system finding) are drawn on purpose and anything else falls through to a plain "label: value" list.

const sessionHref = (workspace: string, id: string) => scopedPath(workspace, `/sessions/${id}`)

/** "over_claim" → "Over claim": a key or enum value in plain words. */
export function plain(key: string): string {
  const s = key.replace(/[_-]+/g, ' ').trim()
  return s ? s[0].toUpperCase() + s.slice(1) : s
}

const str = (v: unknown): string => (typeof v === 'string' ? v.trim() : typeof v === 'number' ? String(v) : '')

const SEVERITY_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 }
const SEVERITY_TONE: Record<string, StatusTone> = { high: 'destructive', medium: 'warning', low: 'neutral' }
const STATUS_TONE: Record<string, StatusTone> = { open: 'info', accepted: 'special', done: 'success', dismissed: 'neutral' }
/** The statuses a person picks between, in lifecycle order; any other the server sends is listed after them. */
const STATUSES = ['open', 'accepted', 'done', 'dismissed']
/** What each status can move to, as the buttons say it. */
const NEXT: Record<string, [BrainOutputStatus, string][]> = {
  open: [['accepted', 'Accept'], ['dismissed', 'Dismiss']],
  accepted: [['done', 'Done'], ['open', 'Reopen']],
  dismissed: [['open', 'Reopen']],
  done: [['open', 'Reopen']],
}
/** How many quotes show before "Show N more". */
const QUOTES_SHOWN = 3
/** The body fields drawn on purpose; every other one is in the fallback list. */
const KNOWN = new Set(['severity', 'type', 'signal', 'fix_belongs', 'what_happened', 'suggested_fix', 'evidence', 'proof',
  'not_checked', 'success_measure', 'merged_from'])

const statusOf = (o: BrainOutput) => o.status || 'open'
const severityOf = (o: BrainOutput) => str(o.body?.severity).toLowerCase()
const repoOf = (o: BrainOutput) => {
  const fb = o.body?.fix_belongs
  return fb && typeof fb === 'object' && !Array.isArray(fb) ? str((fb as Record<string, unknown>).repo) : ''
}
const lastSeen = (o: BrainOutput) => o.last_seen_at || o.created_at

// A faded finding is open or accepted but no run re-saw it within its target's fade window (the server's `faded`); it
// drops into its own collapsed group below the rest, and Keep holds it open. A merged duplicate (status `duplicate`,
// `merged_into` the one it joined) is not listed: the server leaves it out and so does this, should one arrive.
const isFaded = (o: BrainOutput) => Boolean(o.faded)
const isDuplicate = (o: BrainOutput) => o.status === 'duplicate' || o.merged_into != null

/** Most severe first, then the most recently seen. */
function compare(a: BrainOutput, b: BrainOutput): number {
  const s = (SEVERITY_RANK[severityOf(a)] ?? 3) - (SEVERITY_RANK[severityOf(b)] ?? 3)
  return s || Date.parse(lastSeen(b)) - Date.parse(lastSeen(a))
}

type Facet = 'status' | 'severity' | 'repo'
const facetOf: Record<Facet, (o: BrainOutput) => string> = { status: statusOf, severity: severityOf, repo: repoOf }
type Filters = Record<Facet, Set<string> | null> // null: every value

function matches(o: BrainOutput, f: Filters, skip?: Facet): boolean {
  return (Object.keys(facetOf) as Facet[]).every((k) => k === skip || f[k] === null || f[k]!.has(facetOf[k](o)))
}

export function Findings({ ws }: { ws: string }): JSX.Element {
  const [rows, setRows] = useState<BrainOutput[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(0)
  const [filters, setFilters] = useState<Filters>({ status: new Set(['open', 'accepted']), severity: null, repo: null })
  useEffect(() => {
    listBrainOutputs(ws).then(setRows, (e: unknown) => setError(e instanceof Error ? e.message : 'Could not load'))
  }, [ws])
  const move = async (id: number, status: BrainOutputStatus) => {
    setBusy(id)
    setError('')
    try {
      const got = await setOutputStatus(ws, id, status)
      setRows((rs) => (rs ?? []).map((o) => (o.id === id ? got : o)))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not change it')
    } finally {
      setBusy(0)
    }
  }
  const all = useMemo(() => (rows ?? []).filter((o) => !isDuplicate(o)), [rows])
  const shown = useMemo(() => all.filter((o) => matches(o, filters)).sort(compare), [all, filters])
  const live = shown.filter((o) => !isFaded(o))
  const faded = shown.filter(isFaded)

  /** Every value a facet takes, with how many findings have it under the other facets' filters. */
  const facet = (k: Facet, order: string[] = []): [string, number][] => {
    const n = new Map<string, number>()
    for (const v of order) n.set(v, 0)
    for (const o of all) if (matches(o, filters, k)) n.set(facetOf[k](o), (n.get(facetOf[k](o)) ?? 0) + 1)
    for (const o of all) if (!n.has(facetOf[k](o))) n.set(facetOf[k](o), 0)
    const rest = [...n.keys()].filter((v) => !order.includes(v)).sort((a, b) => (n.get(b)! - n.get(a)!) || a.localeCompare(b))
    return [...order, ...rest].map((v) => [v, n.get(v) ?? 0])
  }
  const toggle = (k: Facet, v: string, values: string[]) => setFilters((f) => {
    const cur = new Set(f[k] ?? values)
    if (cur.has(v)) cur.delete(v)
    else cur.add(v)
    return { ...f, [k]: cur.size === values.length ? null : cur }
  })

  return (
    <section className="space-y-3" aria-label="Findings">
      <p className="text-xs text-muted-foreground">
        What learning found that you may see. A finding changes nothing by itself: accept one when someone takes it up,
        mark it done once the fix ships, or dismiss it.
      </p>
      {error && <p className="text-xs text-destructive">{error}</p>}
      {!rows ? (
        error ? null : <p className="text-xs text-muted-foreground">Loading…</p>
      ) : all.length === 0 ? (
        <p className="text-xs text-muted-foreground">Nothing yet.</p>
      ) : (
        <>
          <div className="space-y-1.5 rounded-xl border border-border bg-card p-3">
            <FacetRow label="Status" k="status" values={facet('status', STATUSES)} filters={filters} toggle={toggle}
                      name={plain} />
            <FacetRow label="Severity" k="severity" values={facet('severity', ['high', 'medium', 'low'])} filters={filters}
                      toggle={toggle} name={(v) => (v ? plain(v) : 'Not given')} />
            <FacetRow label="Fix belongs in" k="repo" values={facet('repo')} filters={filters} toggle={toggle}
                      name={(v) => v || 'Not named'} />
          </div>
          <p className="text-xs text-muted-foreground" role="status">
            {shown.length} of {all.length} shown, most severe first, then the most recently seen.
          </p>
          {live.length === 0 && faded.length === 0 ? (
            <p className="text-xs text-muted-foreground">None match these filters.</p>
          ) : (
            <ul className="space-y-3">
              {live.map((o) => <FindingCard key={o.id} o={o} busy={busy === o.id} onMove={(s) => void move(o.id, s)} />)}
            </ul>
          )}
          {faded.length > 0 && (
            <details className="rounded-xl border border-border p-3">
              <summary className="cursor-pointer text-[13px] text-muted-foreground">
                Faded: not seen again lately ({faded.length})
              </summary>
              <ul className="mt-3 space-y-3">
                {faded.map((o) => <FindingCard key={o.id} o={o} busy={busy === o.id} onMove={(s) => void move(o.id, s)} />)}
              </ul>
            </details>
          )}
        </>
      )}
    </section>
  )
}

function FacetRow({ label, k, values, filters, toggle, name }: {
  label: string; k: Facet; values: [string, number][]; filters: Filters
  toggle: (k: Facet, v: string, values: string[]) => void; name: (v: string) => string
}): JSX.Element | null {
  if (values.length < 2 && k !== 'status') return null
  const keys = values.map(([v]) => v)
  return (
    <div role="group" aria-label={label} className="flex flex-wrap items-center gap-1.5 text-xs">
      <span className="w-24 shrink-0 text-muted-foreground">{label}</span>
      {values.map(([v, n]) => {
        const on = filters[k] === null || filters[k]!.has(v)
        return (
          <button key={v || '-'} type="button" aria-pressed={on} onClick={() => toggle(k, v, keys)}
                  className={`rounded-full border px-2 py-0.5 ${on ? 'border-primary/40 bg-primary/10 text-foreground'
                    : 'border-border bg-background text-muted-foreground hover:text-foreground'}`}>
            {name(v)} <span className="tabular-nums text-muted-foreground">{n}</span>
          </button>
        )
      })}
    </div>
  )
}

type Session = NonNullable<BrainOutput['sessions']>[number]

function FindingCard({ o, busy, onMove }: { o: BrainOutput; busy: boolean; onMove: (s: BrainOutputStatus) => void }): JSX.Element {
  const body = (o.body ?? {}) as Record<string, unknown>
  const severity = severityOf(o)
  const status = statusOf(o)
  const fb = body.fix_belongs && typeof body.fix_belongs === 'object' && !Array.isArray(body.fix_belongs)
    ? (body.fix_belongs as Record<string, unknown>) : {}
  const where = [str(fb.repo), str(fb.agent), str(fb.skill)].filter((v, i, all) => v && v !== all[i - 1])
  const bySource = new Map<number, Session>((o.source_sessions ?? []).map((s) => [s.source, s]))
  const evidence = Array.isArray(body.evidence) ? body.evidence : []
  const [allQuotes, setAllQuotes] = useState(false)
  const evidenceRef = useRef<HTMLDivElement>(null)
  const showEvidence = () => {
    setAllQuotes(true)
    evidenceRef.current?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' })
  }
  const seenSinceDismissed = status === 'dismissed' && !!o.status_at && !!o.last_seen_at &&
    Date.parse(o.last_seen_at) > Date.parse(o.status_at)
  const rest = Object.entries(body).filter(([k, v]) => !KNOWN.has(k) && v !== null && v !== '' &&
    !(Array.isArray(v) && v.length === 0))
  return (
    <li className="space-y-2 rounded-xl border border-border bg-card p-4 text-[13px]" aria-label={o.title || plain(o.kind)}>
      <h3 className="text-sm font-semibold text-foreground">{o.title || plain(o.kind)}</h3>
      <div className="flex flex-wrap items-center gap-1.5">
        {severity && <StatusBadge tone={SEVERITY_TONE[severity] ?? 'neutral'}>{plain(severity)} severity</StatusBadge>}
        <StatusBadge tone={STATUS_TONE[status] ?? 'neutral'}>{plain(status)}</StatusBadge>
        {str(body.type) && <StatusBadge tone="neutral">{plain(str(body.type))}</StatusBadge>}
        {str(body.signal) && <StatusBadge tone="neutral">{plain(str(body.signal))}</StatusBadge>}
        {o.kind !== 'finding' && <StatusBadge tone="neutral">{plain(o.kind)}</StatusBadge>}
        {isFaded(o) && <StatusBadge tone="neutral">Faded</StatusBadge>}
        {o.came_back && <StatusBadge tone="warning">Came back after done</StatusBadge>}
        {seenSinceDismissed && <StatusBadge tone="neutral">Seen again since dismissed</StatusBadge>}
        <span className="text-xs text-muted-foreground">
          {(o.seen ?? 1) > 1 ? `seen ${o.seen} times` : 'seen once'} · last seen{' '}
          {relativeAge(lastSeen(o), undefined, { seconds: false })}
        </span>
      </div>
      {((o.sessions_seen ?? 0) > 0 || (o.sightings ?? []).length > 0) && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <LineageStrip o={o} onEvidence={evidence.length > 0 ? showEvidence : undefined} />
          <Sightings times={o.sightings ?? []} />
        </div>
      )}
      {where.length > 0 && (
        <p className="text-foreground">
          <span className="text-muted-foreground">Fix belongs in: </span>
          <span className="font-medium">{where.join(' · ')}</span>
        </p>
      )}
      {str(body.what_happened) && <Field label="What happened">{str(body.what_happened)}</Field>}
      {str(body.suggested_fix) && <Field label="Suggested fix">{str(body.suggested_fix)}</Field>}
      {evidence.length > 0 && (
        <div ref={evidenceRef}>
          <Evidence items={evidence} bySource={bySource} open={allQuotes} setOpen={setAllQuotes} />
        </div>
      )}
      {str(body.success_measure) && <Field label="How to tell it is fixed">{str(body.success_measure)}</Field>}
      {mergedTitles(body.merged_from).length > 0 && (
        <Field label="Merged from">{mergedTitles(body.merged_from).join('\n')}</Field>
      )}
      {rest.length > 0 && (
        <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-xs">
          {rest.map(([k, v]) => (
            <Fragment key={k}>
              <dt className="text-muted-foreground">{plain(k)}</dt>
              <dd className="whitespace-pre-wrap break-words text-foreground">{shown(v)}</dd>
            </Fragment>
          ))}
        </dl>
      )}
      {(str(body.proof) || str(body.not_checked)) && (
        <div className="space-y-0.5 text-xs text-muted-foreground">
          {str(body.proof) && <p>How sure: {str(body.proof)}</p>}
          {str(body.not_checked) && <p>Not checked: {str(body.not_checked)}</p>}
        </div>
      )}
      <Sources o={o} />
      {o.may_set_status && (NEXT[status] ?? []).length > 0 && (
        <div className="flex flex-wrap gap-1.5 pt-1">
          {isFaded(o) && (
            <Button size="sm" variant="outline" disabled={busy} title="No run has seen it lately; keep it open anyway"
                    onClick={() => onMove(status as BrainOutputStatus)}>Keep</Button>
          )}
          {(NEXT[status] ?? []).map(([to, label]) => (
            <Button key={to} size="sm" variant={to === 'accepted' || to === 'done' ? 'outline' : 'ghost'} disabled={busy}
                    onClick={() => onMove(to)}>{label}</Button>
          ))}
        </div>
      )}
    </li>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }): JSX.Element {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="whitespace-pre-wrap break-words text-foreground">{children}</p>
    </div>
  )
}

/** Quoted lines, each with the session it is from: a link where the viewer reads that session, else said plainly. */
function Evidence({ items, bySource, open, setOpen }: {
  items: unknown[]; bySource: Map<number, Session>; open: boolean; setOpen: (v: boolean) => void
}): JSX.Element {
  const list = open ? items : items.slice(0, QUOTES_SHOWN)
  return (
    <div className="space-y-1">
      <p className="text-xs text-muted-foreground">Evidence</p>
      <ul className="space-y-1.5">
        {list.map((e, i) => {
          if (!e || typeof e !== 'object' || Array.isArray(e)) {
            return <li key={i} className="border-l-2 border-border pl-2 text-foreground">{shown(e)}</li>
          }
          const q = e as Record<string, unknown>
          const s = typeof q.source === 'number' ? bySource.get(q.source) : undefined
          return (
            <li key={i} className="border-l-2 border-border pl-2">
              <q className="whitespace-pre-wrap break-words text-foreground">{str(q.quote) || '(no quote)'}</q>
              <span className="block text-xs text-muted-foreground">
                {s ? (
                  <>
                    <Link className="underline" to={sessionHref(s.workspace, s.session_id)}>{s.title || 'untitled session'}</Link>
                  </>
                ) : 'a session you do not read'}
              </span>
            </li>
          )
        })}
      </ul>
      {items.length > QUOTES_SHOWN && (
        <button type="button" className="text-xs underline" onClick={() => setOpen(!open)}>
          {open ? 'Show fewer quotes' : `Show ${items.length - QUOTES_SHOWN} more quote${items.length - QUOTES_SHOWN === 1 ? '' : 's'}`}
        </button>
      )}
    </div>
  )
}

/** The learner that found it and how many sessions it came from, in the footer; the sessions behind a disclosure. */
function Sources({ o }: { o: BrainOutput }): JSX.Element {
  const sessions = o.sessions ?? []
  const hidden = o.hidden_sessions ?? 0
  const total = sessions.length + hidden
  const age = <>{' · first seen '}{relativeAge(o.created_at, undefined, { seconds: false })}</>
  if (total === 0) return <p className="text-xs text-muted-foreground">Found by {o.learner}{age}</p>
  return (
    <details className="text-xs text-muted-foreground">
      <summary className="cursor-pointer">
        Found by {o.learner} in {total} session{total === 1 ? '' : 's'}{age}
      </summary>
      <p className="mt-1">
        {sessions.map((s, i) => (
          <Fragment key={s.session_id}>
            {i > 0 && ', '}
            <Link className="underline" to={sessionHref(s.workspace, s.session_id)}>{s.title || 'untitled'}</Link>
          </Fragment>
        ))}
        {hidden > 0 && `${sessions.length > 0 ? ' and ' : ''}${hidden} session${hidden === 1 ? '' : 's'} you do not read`}
      </p>
    </details>
  )
}

/** The titles of the findings merged into this one (`merged_from`, notes the merge wrote), in words. */
function mergedTitles(v: unknown): string[] {
  const notes = Array.isArray(v) ? v : v && typeof v === 'object' ? [v] : []
  return notes.map((n) => (n && typeof n === 'object' ? str((n as Record<string, unknown>).title) : str(n)))
    .filter(Boolean)
}

/** Any body value in words: a list joined, an object as "key: value" pairs, never "[object Object]". */
export function shown(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (Array.isArray(v)) return v.map(shown).filter(Boolean).join(v.some((x) => x && typeof x === 'object') ? '\n' : ', ')
  if (typeof v === 'object') {
    return Object.entries(v as Record<string, unknown>).filter(([, x]) => x !== null && x !== '')
      .map(([k, x]) => `${plain(k)}: ${shown(x)}`).join(', ')
  }
  return String(v)
}
