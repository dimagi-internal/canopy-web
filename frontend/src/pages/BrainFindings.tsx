import { Fragment, useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Button, StatusBadge } from 'canopy-ui/ui'
import type { StatusTone } from 'canopy-ui/lib'

import {
  askFix, getFixPlan, listBrainOutputs, setOutputStatus, type BrainFixPlan, type BrainOutput, type BrainOutputStatus,
} from '@/api/brain'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'
import { LineageStrip, Sightings } from './BrainFindingViz'

// The Findings tab of the brain page: what learning found, as a list to scan and a finding to act on. Each finding is
// one row (severity, title, where the fix belongs, how widely it was seen); opening it shows what is wrong, the fix,
// how to tell it is fixed and the quoted evidence, beside the one thing to do with it: ask the learner's operator agent
// to fix it (`brain.fixes`, the button saying exactly what that starts and the brief it sends), or say it is already
// fixed or not worth fixing. Everything else a run wrote is behind "More about this finding". The filters are
// dropdowns over status, severity and where the fix belongs, plus a text search.

const sessionHref = (workspace: string, id: string) => scopedPath(workspace, `/sessions/${id}`)

/** "over_claim" → "Over claim": a key or enum value in plain words. */
export function plain(key: string): string {
  const s = key.replace(/[_-]+/g, ' ').trim()
  return s ? s[0].toUpperCase() + s.slice(1) : s
}

const str = (v: unknown): string => (typeof v === 'string' ? v.trim() : typeof v === 'number' ? String(v) : '')

const SEVERITY_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 }
const SEVERITY_DOT: Record<string, string> = { high: 'bg-destructive', medium: 'bg-warning', low: 'bg-muted-foreground' }
/** A status in words: `accepted` is a finding someone asked to be fixed. */
const STATUS_WORD: Record<string, string> = { open: 'Open', accepted: 'Fixing', done: 'Done', dismissed: 'Dismissed' }
const STATUS_TONE: Record<string, StatusTone> = { open: 'info', accepted: 'special', done: 'success', dismissed: 'neutral' }
/** The status filter's choices: the working set (open and fixing) first. */
const STATUS_VIEWS: [string, string, (s: string) => boolean][] = [
  ['working', 'Open and fixing', (s) => s === 'open' || s === 'accepted'],
  ['open', 'Open', (s) => s === 'open'],
  ['accepted', 'Fixing', (s) => s === 'accepted'],
  ['done', 'Done', (s) => s === 'done'],
  ['dismissed', 'Dismissed', (s) => s === 'dismissed'],
  ['all', 'Everything', () => true],
]
/** How many quotes show before "Show N more". */
const QUOTES_SHOWN = 2
/** The body fields drawn on purpose; every other one is under "More about this finding". */
const KNOWN = new Set(['severity', 'type', 'signal', 'fix_belongs', 'what_happened', 'suggested_fix', 'evidence', 'proof',
  'not_checked', 'success_measure', 'merged_from'])

const statusOf = (o: BrainOutput) => o.status || 'open'
const severityOf = (o: BrainOutput) => str(o.body?.severity).toLowerCase()
const fixBelongs = (o: BrainOutput): Record<string, unknown> => {
  const fb = o.body?.fix_belongs
  return fb && typeof fb === 'object' && !Array.isArray(fb) ? (fb as Record<string, unknown>) : {}
}
const repoOf = (o: BrainOutput) => str(fixBelongs(o).repo)
const whereOf = (o: BrainOutput) => {
  const fb = fixBelongs(o)
  return [str(fb.repo), str(fb.agent), str(fb.skill)].filter((v, i, all) => v && !all.slice(0, i).includes(v)).join(' · ')
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

type Filters = { status: string; severity: string; repo: string; q: string }
const ANY = ''

function matches(o: BrainOutput, f: Filters, skip?: keyof Filters): boolean {
  const view = STATUS_VIEWS.find(([k]) => k === f.status)
  if (skip !== 'status' && view && !view[2](statusOf(o))) return false
  if (skip !== 'severity' && f.severity !== ANY && severityOf(o) !== f.severity) return false
  if (skip !== 'repo' && f.repo !== ANY && repoOf(o) !== f.repo) return false
  if (f.q.trim()) {
    const words = f.q.trim().toLowerCase().split(/\s+/)
    const text = `${o.title} ${str(o.body?.what_happened)} ${str(o.body?.suggested_fix)} ${whereOf(o)}`.toLowerCase()
    if (!words.every((w) => text.includes(w))) return false
  }
  return true
}

export function Findings({ ws }: { ws: string }): JSX.Element {
  const [rows, setRows] = useState<BrainOutput[] | null>(null)
  const [error, setError] = useState('')
  const [filters, setFilters] = useState<Filters>({ status: 'working', severity: ANY, repo: ANY, q: '' })
  const [openId, setOpenId] = useState(0)
  useEffect(() => {
    listBrainOutputs(ws).then(setRows, (e: unknown) => setError(e instanceof Error ? e.message : 'Could not load'))
  }, [ws])
  const replace = (got: BrainOutput) => setRows((rs) => (rs ?? []).map((o) => (o.id === got.id ? got : o)))
  const all = useMemo(() => (rows ?? []).filter((o) => !isDuplicate(o)), [rows])
  const shown = useMemo(() => all.filter((o) => matches(o, filters)).sort(compare), [all, filters])
  const live = shown.filter((o) => !isFaded(o))
  const faded = shown.filter(isFaded)
  const count = (skip: keyof Filters, test: (o: BrainOutput) => boolean) =>
    all.filter((o) => matches(o, filters, skip) && test(o)).length
  const repos = useMemo(() => [...new Set(all.map(repoOf))].sort((a, b) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b))),
    [all])
  const set = (k: keyof Filters) => (v: string) => setFilters((f) => ({ ...f, [k]: v }))
  const row = (o: BrainOutput) => (
    <FindingRow key={o.id} ws={ws} o={o} open={openId === o.id} onToggle={() => setOpenId(openId === o.id ? 0 : o.id)}
                onChange={replace} onError={setError} />
  )

  return (
    <section className="space-y-3" aria-label="Findings">
      <p className="text-xs text-muted-foreground">
        What learning found that you may see. Open one to read it and decide: ask for it to be fixed, or say it is
        already fixed or not worth fixing.
      </p>
      {error && <p className="text-xs text-destructive" role="alert">{error}</p>}
      {!rows ? (
        error ? null : <p className="text-xs text-muted-foreground">Loading…</p>
      ) : all.length === 0 ? (
        <p className="text-xs text-muted-foreground">Nothing yet.</p>
      ) : (
        <>
          <div role="search" aria-label="Filter findings" className="flex flex-wrap items-end gap-2">
            <label className="flex min-w-[12rem] flex-1 flex-col gap-0.5 text-xs text-muted-foreground">
              Search
              <input type="search" value={filters.q} onChange={(e) => set('q')(e.target.value)} placeholder="Words in a finding"
                     className="rounded-md border border-input bg-background px-2 py-1 text-[13px] text-foreground" />
            </label>
            <Select label="Status" value={filters.status} onChange={set('status')}
                    options={STATUS_VIEWS.map(([k, word, test]) => [k, `${word} (${count('status', (o) => test(statusOf(o)))})`])} />
            <Select label="Severity" value={filters.severity} onChange={set('severity')}
                    options={[[ANY, `Any (${count('severity', () => true)})`],
                      ...['high', 'medium', 'low'].map((s): [string, string] =>
                        [s, `${plain(s)} (${count('severity', (o) => severityOf(o) === s)})`])]} />
            <Select label="Fix belongs in" value={filters.repo} onChange={set('repo')}
                    options={[[ANY, `Anywhere (${count('repo', () => true)})`],
                      ...repos.map((r): [string, string] => [r, `${r || 'Not named'} (${count('repo', (o) => repoOf(o) === r)})`])]} />
          </div>
          <p className="text-xs text-muted-foreground" role="status">
            {shown.length} of {all.length} shown, most severe first, then the most recently seen.
          </p>
          {live.length === 0 && faded.length === 0 ? (
            <p className="text-xs text-muted-foreground">None match these filters.</p>
          ) : (
            <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-card">{live.map(row)}</ul>
          )}
          {faded.length > 0 && (
            <details className="rounded-xl border border-border p-3">
              <summary className="cursor-pointer text-[13px] text-muted-foreground">
                Faded: not seen again lately ({faded.length})
              </summary>
              <ul className="mt-3 divide-y divide-border overflow-hidden rounded-xl border border-border bg-card">
                {faded.map(row)}
              </ul>
            </details>
          )}
        </>
      )}
    </section>
  )
}

function Select({ label, value, onChange, options }: {
  label: string; value: string; onChange: (v: string) => void; options: [string, string][]
}): JSX.Element {
  return (
    <label className="flex flex-col gap-0.5 text-xs text-muted-foreground">
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value)}
              className="rounded-md border border-input bg-background px-2 py-1 text-[13px] text-foreground">
        {options.map(([v, text]) => <option key={v || '-'} value={v}>{text}</option>)}
      </select>
    </label>
  )
}

type Session = NonNullable<BrainOutput['sessions']>[number]

function FindingRow({ ws, o, open, onToggle, onChange, onError }: {
  ws: string; o: BrainOutput; open: boolean; onToggle: () => void; onChange: (o: BrainOutput) => void
  onError: (e: string) => void
}): JSX.Element {
  const severity = severityOf(o)
  const status = statusOf(o)
  const title = o.title || plain(o.kind)
  const where = repoOf(o)
  const sessions = o.sessions_seen ?? 0
  const people = o.people_seen ?? 0
  const meta = [
    where,
    sessions > 0 ? `${sessions} session${sessions === 1 ? '' : 's'}` : '',
    people > 0 ? `${people} ${people === 1 ? 'person' : 'people'}` : '',
    `last seen ${relativeAge(lastSeen(o), undefined, { seconds: false })}`,
  ].filter(Boolean).join(' · ')
  const detailId = `finding-${o.id}`
  return (
    <li aria-label={title} className="text-[13px]">
      <button type="button" aria-expanded={open} aria-controls={detailId} onClick={onToggle}
              className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-muted/40">
        <span className="mt-1.5 flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground sm:w-16">
          <span aria-hidden className={`h-2 w-2 rounded-full ${SEVERITY_DOT[severity] ?? 'bg-border'}`} />
          <span className="sr-only sm:not-sr-only">{severity ? plain(severity) : 'No severity'}</span>
        </span>
        <span className="min-w-0 flex-1 space-y-0.5">
          <h3 className={`text-sm font-medium text-foreground ${open ? '' : 'line-clamp-2'}`}>{title}</h3>
          <span className="block text-xs text-muted-foreground">{meta}</span>
        </span>
        <span className="flex shrink-0 flex-wrap justify-end gap-1">
          {status !== 'open' && <StatusBadge tone={STATUS_TONE[status] ?? 'neutral'}>{STATUS_WORD[status] ?? plain(status)}</StatusBadge>}
          {o.came_back && <StatusBadge tone="warning">Came back</StatusBadge>}
        </span>
      </button>
      {open && (
        <div id={detailId} className="grid gap-5 border-t border-border bg-background/40 px-4 py-4 md:grid-cols-[minmax(0,1fr)_17rem]">
          <FindingDetail o={o} />
          <Decide ws={ws} o={o} onChange={onChange} onError={onError} />
        </div>
      )}
    </li>
  )
}

function FindingDetail({ o }: { o: BrainOutput }): JSX.Element {
  const body = (o.body ?? {}) as Record<string, unknown>
  const bySource = new Map<number, Session>((o.source_sessions ?? []).map((s) => [s.source, s]))
  const evidence = Array.isArray(body.evidence) ? body.evidence : []
  const [allQuotes, setAllQuotes] = useState(false)
  const evidenceRef = useRef<HTMLDivElement>(null)
  const showEvidence = () => {
    setAllQuotes(true)
    evidenceRef.current?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' })
  }
  const status = statusOf(o)
  const seenSinceDismissed = status === 'dismissed' && !!o.status_at && !!o.last_seen_at &&
    Date.parse(o.last_seen_at) > Date.parse(o.status_at)
  const rest = Object.entries(body).filter(([k, v]) => !KNOWN.has(k) && v !== null && v !== '' &&
    !(Array.isArray(v) && v.length === 0))
  const merged = mergedTitles(body.merged_from)
  const where = whereOf(o)
  return (
    <div className="min-w-0 space-y-4">
      {str(body.what_happened) && <Field label="What's wrong">{str(body.what_happened)}</Field>}
      {(str(body.suggested_fix) || where) && (
        <Field label="The fix">
          {str(body.suggested_fix)}
          {where && <span className="mt-1 block text-xs text-muted-foreground">Belongs in {where}</span>}
        </Field>
      )}
      {str(body.success_measure) && <Field label="Fixed when">{str(body.success_measure)}</Field>}
      {evidence.length > 0 && (
        <div ref={evidenceRef}>
          <Evidence items={evidence} bySource={bySource} open={allQuotes} setOpen={setAllQuotes} />
        </div>
      )}
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer">More about this finding</summary>
        <div className="mt-2 space-y-2">
          {((o.sessions_seen ?? 0) > 0 || (o.sightings ?? []).length > 0) && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
              <LineageStrip o={o} onEvidence={evidence.length > 0 ? showEvidence : undefined} />
              <Sightings times={o.sightings ?? []} />
            </div>
          )}
          <p>
            {(o.seen ?? 1) > 1 ? `Seen ${o.seen} times` : 'Seen once'}
            {str(body.type) && ` · ${plain(str(body.type))}`}
            {str(body.signal) && ` · ${plain(str(body.signal))}`}
            {o.kind !== 'finding' && ` · ${plain(o.kind)}`}
            {o.came_back && ' · came back after it was done'}
            {seenSinceDismissed && ' · seen again since it was dismissed'}
          </p>
          {str(body.proof) && <p>How sure: {str(body.proof)}</p>}
          {str(body.not_checked) && <p>Not checked: {str(body.not_checked)}</p>}
          {merged.length > 0 && <p className="whitespace-pre-wrap">Merged from: {merged.join('\n')}</p>}
          {rest.length > 0 && (
            <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5">
              {rest.map(([k, v]) => (
                <Fragment key={k}>
                  <dt>{plain(k)}</dt>
                  <dd className="whitespace-pre-wrap break-words text-foreground">{shown(v)}</dd>
                </Fragment>
              ))}
            </dl>
          )}
          <Sources o={o} />
        </div>
      </details>
    </div>
  )
}

/** The one decision a finding asks for, each choice saying what it does. */
function Decide({ ws, o, onChange, onError }: {
  ws: string; o: BrainOutput; onChange: (o: BrainOutput) => void; onError: (e: string) => void
}): JSX.Element {
  const status = statusOf(o)
  const [plan, setPlan] = useState<BrainFixPlan | null>(null)
  const [planError, setPlanError] = useState('')
  const [busy, setBusy] = useState(false)
  const may = Boolean(o.may_set_status)
  const fixer = o.fixer?.name ?? ''
  const mayAsk = Boolean(o.may_request_fix)
  useEffect(() => {
    if (!mayAsk || status !== 'open') return
    let live = true
    getFixPlan(ws, o.id).then(
      (p) => { if (live) setPlan(p) },
      (e: unknown) => { if (live) setPlanError(e instanceof Error ? e.message : 'Could not load what it starts') })
    return () => { live = false }
  }, [ws, o.id, mayAsk, status])
  const run = async (work: () => Promise<BrainOutput>) => {
    setBusy(true)
    onError('')
    try {
      onChange(await work())
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Could not do that')
    } finally {
      setBusy(false)
    }
  }
  const move = (to: BrainOutputStatus) => void run(() => setOutputStatus(ws, o.id, to))
  const fix = () => void run(async () => (await askFix(ws, o.id)).output as BrainOutput)
  const conversation = o.fix_session ? <Link className="underline" to={o.fix_session}>the conversation</Link> : null

  return (
    <aside aria-label="What to do with it" className="space-y-3 md:border-l md:border-border md:pl-5">
      {status === 'accepted' && (
        <p className="text-foreground">
          {fixer ? `${fixer} is fixing this` : 'Someone is fixing this'}{conversation ? <>: {conversation}.</> : '.'}
        </p>
      )}
      {status === 'done' && <p className="text-foreground">Done{o.status_by ? `, marked by ${o.status_by}` : ''}.</p>}
      {status === 'dismissed' && <p className="text-foreground">Dismissed{o.status_by ? ` by ${o.status_by}` : ''}.</p>}
      {!may ? (
        status === 'open' && <p className="text-xs text-muted-foreground">Its workspace's owners and admins decide what happens to it.</p>
      ) : status === 'open' ? (
        <>
          {o.fixer && !mayAsk && (
            <p className="text-xs text-muted-foreground">
              Asking {fixer} to fix it is for a person who decides on it, signed in as themselves.
            </p>
          )}
          {mayAsk && (
            <Choice label={plan?.label || `Ask ${fixer} to fix this`} primary disabled={busy || !plan} onClick={fix}
                    note={plan?.what_happens ?? (planError || 'Loading what this starts…')}>
              {plan?.brief && (
                <details className="text-xs text-muted-foreground">
                  <summary className="cursor-pointer">Read the message {fixer} gets</summary>
                  <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-background p-2 text-[11px] text-foreground">
                    {plan.brief}
                  </pre>
                </details>
              )}
            </Choice>
          )}
          <Choice label="Already fixed" disabled={busy} onClick={() => move('done')}
                  note="Marks it done. If learning sees it again, it comes back flagged as a repeat." />
          <Choice label="Not worth fixing" disabled={busy} onClick={() => move('dismissed')}
                  note="Dismisses it. Learning folds repeats into it for a while instead of raising it again." />
          {isFaded(o) && (
            <Choice label="Keep" disabled={busy} onClick={() => move('open')}
                    note="No run has seen it lately. Keeps it in the list anyway." />
          )}
        </>
      ) : status === 'accepted' ? (
        <>
          <Choice label="Mark done" disabled={busy} onClick={() => move('done')}
                  note={`Once the fix has shipped. ${fixer || 'The agent'} is asked to do this itself when it does.`} />
          <Choice label="Stop fixing" disabled={busy} onClick={() => move('open')}
                  note="Back to open. It does not stop the conversation: tell the agent there." />
        </>
      ) : (
        <Choice label="Reopen" disabled={busy} onClick={() => move('open')} note="Back to open, to decide again." />
      )}
    </aside>
  )
}

function Choice({ label, note, onClick, disabled, primary, children }: {
  label: string; note: string; onClick: () => void; disabled?: boolean; primary?: boolean; children?: ReactNode
}): JSX.Element {
  return (
    <div className="space-y-1">
      <Button size="sm" variant={primary ? 'default' : 'outline'} disabled={disabled} onClick={onClick}
              className="w-full justify-center">{label}</Button>
      <p className="text-xs text-muted-foreground">{note}</p>
      {children}
    </div>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }): JSX.Element {
  return (
    <div className="space-y-0.5">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
      <div className="whitespace-pre-wrap break-words leading-relaxed text-foreground">{children}</div>
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
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Evidence</p>
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
                {s ? <Link className="underline" to={sessionHref(s.workspace, s.session_id)}>{s.title || 'untitled session'}</Link>
                  : 'a session you do not read'}
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

/** The learner that found it and how many sessions it came from; the sessions behind a disclosure. */
function Sources({ o }: { o: BrainOutput }): JSX.Element {
  const sessions = o.sessions ?? []
  const hidden = o.hidden_sessions ?? 0
  const total = sessions.length + hidden
  const age = <>{' · first seen '}{relativeAge(o.created_at, undefined, { seconds: false })}</>
  if (total === 0) return <p>Found by {o.learner}{age}</p>
  return (
    <details>
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
