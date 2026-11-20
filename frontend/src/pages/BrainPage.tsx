import { Fragment, useCallback, useEffect, useState, type JSX, type ReactNode } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Button, CopyButton, StatusBadge, Tabs, TabsList, TabsTrigger } from 'canopy-ui/ui'

import {
  createBrainPreview,
  deleteBrainPreview,
  getBrainLinks,
  getBrainRuns,
  getBrainState,
  getPreviewTranscript,
  getWouldRead,
  listBrainOutputs,
  listBrainPreviews,
  type BrainOutput,
  type BrainPreview,
  type BrainRun,
  type BrainSource,
  type BrainState,
  type BrainStoredLink,
  type PreviewTranscript,
  type WouldRead,
  type WouldReadRow,
} from '@/api/brain'
import { BrainSourceBanner, useBrainSource } from '@/brain/source'
import { Td, Th, Tr } from '@/components/DataTable'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'

// THE BRAIN: canopy learning as it stands, from a SOURCE: live, or one of your preview snapshots (what the brain would
// do, `?preview=<id>`). The screens are the same either way; only the banner says which. It renders; it never decides:
// every verdict, count and grouping is the server's (`apps/brain/view.py`). Person-only for a preview: its routes
// refuse an agent, a turn's token and an MCP client, and answer only with sessions and projects you still read.

const CLASSES = ['individual', 'memory', 'project', 'system'] as const

/** A session's page, in the same source. */
function useSessionHref() {
  const { withSource } = useBrainSource()
  return (workspace: string, id: string) => withSource(scopedPath(workspace, `/sessions/${id}`))
}

export function BrainPage(): JSX.Element {
  const [search, setSearch] = useSearchParams()
  const tab = search.get('tab') === 'sessions' ? 'sessions' : 'now'
  const { source, preview } = useBrainSource()
  const [state, setState] = useState<BrainState | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    setState(null)
    setError('')
    getBrainState(source).then(setState, (e: unknown) => setError(e instanceof Error ? e.message : 'Could not load'))
  }, [source])

  const pick = (next: string) => {
    const params = new URLSearchParams(search)
    if (next === 'sessions') params.set('tab', 'sessions')
    else params.delete('tab')
    setSearch(params)
  }

  const home = state?.workspaces[0]
  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4 sm:p-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-foreground">Canopy Brain</h1>
          <p className="text-[13px] text-muted-foreground">
            What canopy may learn from, who agreed, and what it would do. Only you see this view of it.
          </p>
        </div>
        <nav aria-label="Related" className="flex flex-wrap gap-3 text-[13px]">
          <Link className="underline" to="/settings/learning">Your learning settings</Link>
          {home && <Link className="underline" to={scopedPath(home, '/network')}>Who works with whom</Link>}
        </nav>
      </header>
      <SourceControls />
      <BrainSourceBanner window={state?.source?.window} />
      <Tabs value={tab} onValueChange={pick}>
        <TabsList>
          <TabsTrigger value="now">Now</TabsTrigger>
          <TabsTrigger value="sessions">Sessions</TabsTrigger>
        </TabsList>
      </Tabs>
      {error ? (
        <p className="text-[13px] text-destructive">{error}</p>
      ) : !state ? (
        <p className="text-[13px] text-muted-foreground">Loading…</p>
      ) : tab === 'sessions' ? (
        <SessionsTab key={source} source={source} />
      ) : (
        <NowTab key={source} state={state} source={source} preview={preview} />
      )}
    </div>
  )
}

// ---- the source: live or one of your previews --------------------------------------------------------------

const WINDOWS = [
  { hours: 24, label: 'Last 24 hours' },
  { hours: 72, label: 'Last 3 days' },
  { hours: 168, label: 'Last 7 days' },
]

const SELECT = 'rounded-md border border-input bg-background px-2 py-1 text-foreground'

function previewLabel(p: BrainPreview): string {
  const made = relativeAge(p.created_at, undefined, { seconds: false })
  const hours = Math.round((Date.parse(p.window.until) - Date.parse(p.window.since)) / 3600_000)
  return `#${p.id} · ${hours >= 48 ? `${Math.round(hours / 24)} days` : `${hours}h`} · ${p.sessions} sessions · ${made}`
}

/** The switcher (live or one of your previews), "Run a preview" and deleting one. */
function SourceControls(): JSX.Element {
  const { preview } = useBrainSource()
  const [search, setSearch] = useSearchParams()
  const navigate = useNavigate()
  const [previews, setPreviews] = useState<BrainPreview[] | null>(null)
  const [hours, setHours] = useState(24)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    listBrainPreviews().then(setPreviews, () => setPreviews([]))
  }, [])
  useEffect(load, [load])

  const choose = (id: string) => {
    const params = new URLSearchParams(search)
    if (id) params.set('preview', id)
    else params.delete('preview')
    setSearch(params)
  }

  const run = async () => {
    setBusy(true)
    setError('')
    try {
      const made = await createBrainPreview(hours)
      load()
      choose(String(made.id))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not run a preview')
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    if (!preview) return
    setBusy(true)
    try {
      await deleteBrainPreview(Number(preview))
      load()
      navigate('/brain')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not delete')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section aria-label="Data source" className="flex flex-wrap items-center gap-3 text-[13px]">
      <label className="flex items-center gap-2 text-muted-foreground">
        Showing
        <select aria-label="Source" className={SELECT} value={preview ?? ''} onChange={(e) => choose(e.target.value)}>
          <option value="">Live</option>
          {(previews ?? []).map((p) => (
            <option key={p.id} value={String(p.id)}>Preview {previewLabel(p)}</option>
          ))}
          {preview && previews && !previews.some((p) => String(p.id) === preview) && (
            <option value={preview}>Preview #{preview}</option>
          )}
        </select>
      </label>
      {preview && (
        <button type="button" className="text-xs underline" disabled={busy} onClick={() => void remove()}>
          Delete this preview
        </button>
      )}
      <span className="flex items-center gap-2">
        <select aria-label="Preview window" className={SELECT} value={hours} onChange={(e) => setHours(Number(e.target.value))}>
          {WINDOWS.map((w) => (
            <option key={w.hours} value={w.hours}>{w.label}</option>
          ))}
        </select>
        <Button size="sm" disabled={busy} onClick={() => void run()}>{busy ? 'Running…' : 'Run a preview'}</Button>
      </span>
      {error && <span className="text-destructive">{error}</span>}
    </section>
  )
}

function Section({ title, note, children }: { title: string; note?: ReactNode; children: ReactNode }): JSX.Element {
  return (
    <section className="space-y-2 rounded-xl border border-border bg-card p-4" aria-label={title}>
      <h2 className="text-sm font-semibold text-foreground">{title}</h2>
      {note && <p className="text-xs text-muted-foreground">{note}</p>}
      {children}
    </section>
  )
}

/** A table that scrolls sideways on a phone instead of breaking the page. */
function Table({ children }: { children: ReactNode }): JSX.Element {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[13px] [&_td]:pr-3 [&_th]:pr-3 [&_td:last-child]:pr-0 [&_th:last-child]:pr-0">{children}</table>
    </div>
  )
}

const Empty = ({ children }: { children: ReactNode }) => <p className="text-xs text-muted-foreground">{children}</p>

const scopeNote = (scope: string) => (scope === 'workspace' ? 'whole workspace' : 'yours only')

function counts(map: Record<string, number>): string {
  const parts = Object.entries(map).map(([k, n]) => `${k.replace(/_/g, ' ')} ${n}`)
  return parts.length ? parts.join(', ') : '—'
}

// ---- Now ---------------------------------------------------------------------------------------------

function NowTab({ state, source, preview }: { state: BrainState; source: BrainSource; preview: string | null }): JSX.Element {
  return (
    <div className="space-y-4">
      <Section title="Rollout">
        <p className="flex flex-wrap items-center gap-2 text-[13px] text-foreground">
          <StatusBadge tone={state.reads_enabled ? 'success' : 'neutral'} dot>
            {state.reads_enabled ? 'Reads on' : 'Reads off'}
          </StatusBadge>
          {state.reads_enabled
            ? 'Learners may read the sessions everyone in them agreed to.'
            : 'No learner reads a session yet. Consent, links and the preview work; nothing is learned.'}
        </p>
      </Section>

      <Section
        title="Consent"
        note={
          <>
            Members who agreed to each class, per workspace: counts, never names. You agreed to{' '}
            {state.consent.mine.length ? state.consent.mine.join(', ') : 'nothing yet'}.
          </>
        }
      >
        <Table>
          <thead>
            <Tr head="label">
              <Th>Workspace</Th>
              <Th align="right">Members</Th>
              <Th align="right">Any</Th>
              {CLASSES.map((c) => (
                <Th key={c} align="right">{c}</Th>
              ))}
            </Tr>
          </thead>
          <tbody>
            {state.consent.workspaces.map((w) => (
              <Tr key={w.workspace} rule="bottom-last">
                <Td>{w.name}</Td>
                <Td numeric align="right">{w.members}</Td>
                <Td numeric align="right">{w.agreed_any}</Td>
                {CLASSES.map((c) => (
                  <Td key={c} numeric align="right">{w.agreed[c] ?? 0}</Td>
                ))}
              </Tr>
            ))}
          </tbody>
        </Table>
      </Section>

      <Section title="Sources" note="What could be learned from: open or withdrawn, and the sealed ones by why.">
        {state.sources.length === 0 ? (
          <Empty>No sources yet.</Empty>
        ) : (
          <Table>
            <thead>
              <Tr head="label">
                <Th>Workspace</Th>
                <Th>Counted</Th>
                <Th align="right">Open</Th>
                <Th align="right">Withdrawn</Th>
                <Th>Sealed</Th>
              </Tr>
            </thead>
            <tbody>
              {state.sources.map((s) => (
                <Tr key={s.workspace} rule="bottom-last">
                  <Td>{s.workspace}</Td>
                  <Td tone="muted">{scopeNote(s.scope)}</Td>
                  <Td numeric align="right">{s.open}</Td>
                  <Td numeric align="right">{s.withdrawn}</Td>
                  <Td tone="muted">{counts(s.sealed)}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Section>

      <StoredLinks state={state} source={source} />

      <Section title="Learners" note="What is registered to learn, whether it is on, how much of a session it reads, and who answers for it.">
        <Table>
          <thead>
            <Tr head="label">
              <Th>Learner</Th>
              <Th>Class</Th>
              <Th>Enabled</Th>
              <Th hideBelow="sm">Scope</Th>
              <Th hideBelow="md">Schedule</Th>
              <Th hideBelow="md">Operator</Th>
              <Th>Accountable admin</Th>
            </Tr>
          </thead>
          <tbody>
            {state.learners.map((l) => (
              <Tr key={l.slug} rule="bottom-last">
                <Td>
                  {l.name} <span className="font-mono text-muted-foreground">{l.slug}</span>
                </Td>
                <Td>{l.purpose_class}</Td>
                <Td>
                  <StatusBadge tone={l.enabled ? 'success' : 'neutral'}>{l.enabled ? 'on' : 'off'}</StatusBadge>
                </Td>
                <Td hideBelow="sm">{l.scope}</Td>
                <Td hideBelow="md" tone="muted">{l.schedule || '—'}</Td>
                <Td hideBelow="md" tone="muted">{l.operator || '—'}</Td>
                <Td tone="muted">{l.accountable || 'nobody yet'}</Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      </Section>

      <Runs state={state} source={source} />
      <Outputs source={source} preview={preview} />

      <Section
        title="Removals and cascade work"
        note="Counts only, never what was removed. Your own removals are listed in your learning settings."
      >
        {state.cascade.length === 0 ? (
          <Empty>Nothing pending, nothing removed in the last seven days.</Empty>
        ) : (
          <Table>
            <thead>
              <Tr head="label">
                <Th>Workspace</Th>
                <Th>Counted</Th>
                <Th align="right">To re-derive</Th>
                <Th align="right">Learners not yet told</Th>
                <Th align="right">Sessions removed, 7d</Th>
                <Th align="right">Outputs withdrawn, 7d</Th>
                <Th align="right">Outputs stale, 7d</Th>
              </Tr>
            </thead>
            <tbody>
              {state.cascade.map((c) => (
                <Tr key={c.workspace} rule="bottom-last">
                  <Td>{c.workspace}</Td>
                  <Td tone="muted">{scopeNote(c.scope)}</Td>
                  <Td numeric align="right">{c.rederive_open}</Td>
                  <Td numeric align="right">{c.revocations_undelivered}</Td>
                  <Td numeric align="right">{c.removed_recently}</Td>
                  <Td numeric align="right">{c.outputs_withdrawn_recently}</Td>
                  <Td numeric align="right">{c.outputs_stale_recently}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Section>
    </div>
  )
}

/** Pages of a list the server pages by `before`. */
function usePaged<T>(load: (before: number) => Promise<{ next_before: number; done: boolean } & Record<string, unknown>>,
                     key: string) {
  const [rows, setRows] = useState<T[]>([])
  const [next, setNext] = useState(0)
  const [done, setDone] = useState(true)
  const [error, setError] = useState('')
  const more = useCallback(
    async (before: number) => {
      try {
        const page = await load(before)
        setRows((r) => (before ? [...r, ...(page[key] as T[])] : (page[key] as T[])))
        setNext(page.next_before)
        setDone(page.done)
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Could not load')
      }
    },
    [load, key],
  )
  useEffect(() => {
    void more(0)
  }, [more])
  return { rows, error, done, loadMore: () => void more(next) }
}

function StoredLinks({ state, source }: { state: BrainState; source: BrainSource }): JSX.Element {
  const load = useCallback((before: number) => getBrainLinks(source, before), [source])
  const { rows, error, done, loadMore } = usePaged<BrainStoredLink>(load, 'links')
  const sessionHref = useSessionHref()
  return (
    <Section title="Project links" note="Which project each session you can read is tied to, and how sure canopy is.">
      {state.links.length === 0 ? (
        <Empty>No project links on sessions you read.</Empty>
      ) : (
        <>
          <ul className="flex flex-wrap gap-2 text-xs">
            {state.links.map((g) => (
              <li key={`${g.workspace}/${g.project}/${g.tier}`} className="rounded-md border border-border px-2 py-1">
                <span className="text-foreground">{g.name}</span>{' '}
                <span className="text-muted-foreground">{g.tier}</span>{' '}
                <span className="tabular-nums text-foreground">{g.count}</span>
              </li>
            ))}
          </ul>
          <Table>
            <thead>
              <Tr head="label">
                <Th>Project</Th>
                <Th>Tier</Th>
                <Th>Session</Th>
                <Th hideBelow="sm">How</Th>
                <Th hideBelow="md">Evidence</Th>
              </Tr>
            </thead>
            <tbody>
              {rows.map((l) => (
                <Tr key={l.id} rule="bottom-last" top>
                  <Td>{l.name}</Td>
                  <Td>{l.tier}</Td>
                  <Td>
                    <Link className="underline" to={sessionHref(l.workspace, l.session_id)}>{l.title || l.session_id}</Link>
                  </Td>
                  <Td hideBelow="sm" tone="muted">
                    {l.how}
                    {l.confidence != null && ` · ${Math.round(l.confidence * 100)}%`}
                  </Td>
                  <Td hideBelow="md" tone="muted">{l.evidence.join(', ') || '—'}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
          {!done && (
            <button type="button" className="text-xs underline" onClick={loadMore}>Load more links</button>
          )}
        </>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </Section>
  )
}

function Runs({ state, source }: { state: BrainState; source: BrainSource }): JSX.Element {
  const load = useCallback((before: number) => getBrainRuns(source, before), [source])
  const { rows, error, done, loadMore } = usePaged<BrainRun>(load, 'runs')
  const sessionHref = useSessionHref()
  const { open, done: finished, failed } = state.runs
  return (
    <Section
      title="Runs"
      note={`${open} open, ${finished} done, ${failed} failed. A run's reads name only sessions you read yourself.`}
    >
      {rows.length === 0 ? (
        <Empty>No runs you can see.</Empty>
      ) : (
        <Table>
          <thead>
            <Tr head="label">
              <Th>Learner</Th>
              <Th>About</Th>
              <Th>State</Th>
              <Th hideBelow="sm">Started</Th>
              <Th>Read</Th>
            </Tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <Tr key={r.id} rule="bottom-last" top>
                <Td>
                  {r.learner} <span className="text-muted-foreground">{r.purpose}</span>
                </Td>
                <Td>{r.subject || r.subject_type}</Td>
                <Td>
                  <StatusBadge tone={r.state === 'failed' ? 'destructive' : r.state === 'open' ? 'info' : 'neutral'}>
                    {r.state}
                  </StatusBadge>
                </Td>
                <Td hideBelow="sm" tone="muted" nowrap>{relativeAge(r.created_at, undefined, { seconds: false })}</Td>
                <Td>
                  <span className="tabular-nums">{r.reads}</span>
                  {r.sessions.length > 0 && ': '}
                  {r.sessions.map((s, i) => (
                    <Fragment key={s.session_id}>
                      {i > 0 && ', '}
                      <Link className="underline" to={sessionHref(s.workspace, s.session_id)}>{s.title || 'untitled'}</Link>
                    </Fragment>
                  ))}
                  {r.hidden > 0 && <span className="text-muted-foreground"> (+{r.hidden} you do not read)</span>}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
      {!done && (
        <button type="button" className="text-xs underline" onClick={loadMore}>Load more runs</button>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </Section>
  )
}

const COMMAND = '/canopy:brain-preview --publish'

function Outputs({ source, preview }: { source: BrainSource; preview: string | null }): JSX.Element {
  const [rows, setRows] = useState<BrainOutput[] | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    listBrainOutputs(source).then(setRows, (e: unknown) => setError(e instanceof Error ? e.message : 'Could not load'))
  }, [source])
  const command = `${COMMAND} ${preview ?? '<preview id>'}`
  return (
    <Section
      title="Outputs you may see"
      note={
        <>
          What learning produced, for the audience its class allows. Recommendations: run{' '}
          <span className="font-mono">{command}</span> to add them{preview ? ' to this preview' : ' to a preview'}.{' '}
          <CopyButton text={command} />
        </>
      }
    >
      {error ? (
        <p className="text-xs text-destructive">{error}</p>
      ) : !rows ? (
        <Empty>Loading…</Empty>
      ) : rows.length === 0 ? (
        <Empty>None yet.</Empty>
      ) : (
        <Table>
          <thead>
            <Tr head="label">
              <Th>Title</Th>
              <Th>Learner</Th>
              <Th hideBelow="sm">About</Th>
              <Th hideBelow="sm">Made</Th>
            </Tr>
          </thead>
          <tbody>
            {rows.map((o) => (
              <Tr key={o.id} rule="bottom-last">
                <Td>
                  {o.title || o.kind}
                  {o.source?.kind === 'preview' && <StatusBadge tone="info" className="ml-2">preview</StatusBadge>}
                </Td>
                <Td tone="muted">{o.learner}</Td>
                <Td hideBelow="sm" tone="muted">{o.subject_type} {o.subject}</Td>
                <Td hideBelow="sm" tone="muted" nowrap>{relativeAge(o.created_at, undefined, { seconds: false })}</Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
    </Section>
  )
}

// ---- Sessions: whether each class could read each session, and the links canopy would make ----------------

function SessionsTab({ source }: { source: BrainSource }): JSX.Element {
  // The cursors of the pages before this one, so Previous goes back without the client counting anything.
  const [cursors, setCursors] = useState<string[]>([''])
  const [data, setData] = useState<WouldRead | null>(null)
  const [error, setError] = useState('')
  const after = cursors[cursors.length - 1]

  useEffect(() => {
    setData(null)
    setError('')
    getWouldRead(source, after).then(setData, (e: unknown) => setError(e instanceof Error ? e.message : 'Could not load'))
  }, [source, after])

  const first = (cursors.length - 1) * 10
  if (error) return <p className="text-[13px] text-destructive">{error}</p>
  if (!data) return <p className="text-[13px] text-muted-foreground">Loading…</p>
  return (
    <div className="space-y-4">
      <Summary data={data} first={first} />
      <WouldLink data={data} />
      <Section title="Sessions" note="Whether each class could read the session now, and if everyone in it agreed.">
        {data.sessions.length === 0 ? (
          <Empty>No sessions you read in this window.</Empty>
        ) : (
          <SessionTable data={data} />
        )}
        <div className="flex gap-3 text-xs">
          {cursors.length > 1 && (
            <button type="button" className="underline" onClick={() => setCursors((c) => c.slice(0, -1))}>
              Previous
            </button>
          )}
          {!data.done && (
            <button type="button" className="underline" onClick={() => setCursors((c) => [...c, data.next_after])}>
              Next
            </button>
          )}
        </div>
      </Section>
    </div>
  )
}

function Tile({ label, value, hint }: { label: string; value: ReactNode; hint?: string }): JSX.Element {
  return (
    <div className="rounded-lg border border-border bg-card px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="text-base font-semibold tabular-nums text-foreground">{value}</div>
      {hint && <div className="text-[11px] text-muted-foreground">{hint}</div>}
    </div>
  )
}

function Summary({ data, first }: { data: WouldRead; first: number }): JSX.Element {
  const c = data.counts
  const pair = (p: { now: number; if_everyone_agreed: number }) => `${p.now} / ${p.if_everyone_agreed}`
  const shown = c.sessions ? `${first + 1}–${first + c.sessions} of ${data.total}` : `0 of ${data.total}`
  return (
    <section aria-label="Summary" className="space-y-1">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Tile label="Sessions" value={data.total} hint={`this page: ${shown}`} />
        <Tile label="Project" value={pair(c.project)} hint="now / if all agreed" />
        <Tile label="System" value={pair(c.system)} hint="now / if all agreed" />
        <Tile label="About you" value={pair(c.individual)} hint="now / if all agreed" />
        <Tile label="Links to store" value={c.links} />
        <Tile label="Sealed" value={Object.values(c.sealed).reduce((a, b) => a + b, 0)} hint={counts(c.sealed)} />
      </div>
      <p className="text-[11px] text-muted-foreground">Counts are for this page of sessions.</p>
    </section>
  )
}

function WouldLink({ data }: { data: WouldRead }): JSX.Element {
  const sessionHref = useSessionHref()
  return (
    <Section title="Links it would store" note="By project and tier, with the evidence; a stored link that keeps its place is named.">
      {data.would_link.length === 0 ? (
        <Empty>None on this page.</Empty>
      ) : (
        <Table>
          <thead>
            <Tr head="label">
              <Th>Project</Th>
              <Th>Tier</Th>
              <Th>Session</Th>
              <Th hideBelow="sm">How</Th>
              <Th>Evidence</Th>
            </Tr>
          </thead>
          <tbody>
            {data.would_link.flatMap((g) =>
              g.sessions.map((s, i) => (
                <Tr key={`${g.workspace}/${g.project}/${g.tier}/${s.id}`} rule="bottom-last" top>
                  <Td>{i === 0 ? g.project : ''}</Td>
                  <Td>{i === 0 ? g.tier : ''}</Td>
                  <Td>
                    <Link className="underline" to={sessionHref(g.workspace, s.id)}>{s.title || 'untitled'}</Link>
                    {s.outranked_by && (
                      <div className="text-[11px] text-muted-foreground">
                        kept out by a stored {String(s.outranked_by.how)} link ({String(s.outranked_by.tier)})
                      </div>
                    )}
                  </Td>
                  <Td hideBelow="sm" tone="muted">{s.how}</Td>
                  <Td tone="muted">{s.evidence.join(', ') || '—'}</Td>
                </Tr>
              )),
            )}
          </tbody>
        </Table>
      )}
    </Section>
  )
}

type Verdict = { ok: boolean; reason: string }
type VerdictPair = { now: Verdict; if_everyone_agreed: Verdict }

/** A reason code with the person it names, by their name in this session. */
function reasonText(reason: string, row: WouldReadRow): string {
  const [code, who] = reason.split(':')
  const person = row.participants.find((p) => String(p.user_id) === who)
  return `${code.replace(/_/g, ' ')}${who ? `: ${person?.name || who}` : ''}`
}

function VerdictCell({ v, row }: { v: Verdict | undefined; row: WouldReadRow }): JSX.Element {
  if (!v) return <span className="text-muted-foreground">—</span>
  return v.ok ? (
    <StatusBadge tone="success">yes</StatusBadge>
  ) : (
    <span>
      <StatusBadge tone="neutral">no</StatusBadge>
      <span className="block text-[11px] text-muted-foreground">{reasonText(v.reason, row)}</span>
    </span>
  )
}

function SessionTable({ data }: { data: WouldRead }): JSX.Element {
  const sessionHref = useSessionHref()
  const [open, setOpen] = useState<string | null>(null)
  const [transcripts, setTranscripts] = useState<Record<string, PreviewTranscript | string>>({})

  const toggle = async (row: WouldReadRow) => {
    if (open === row.id) return setOpen(null)
    setOpen(row.id)
    if (transcripts[row.id]) return
    try {
      const t = await getPreviewTranscript(data.window.since, data.window.until, row.id)
      setTranscripts((m) => ({ ...m, [row.id]: t }))
    } catch (e) {
      setTranscripts((m) => ({ ...m, [row.id]: e instanceof Error ? e.message : 'Could not load' }))
    }
  }

  return (
    <Table>
      <thead>
        <Tr head="label">
          <Th>Session</Th>
          <Th hideBelow="md">People</Th>
          <Th>Project now</Th>
          <Th>Project if all agreed</Th>
          <Th hideBelow="sm">System now</Th>
          <Th hideBelow="sm">System if all agreed</Th>
          <Th hideBelow="lg">About you now</Th>
          <Th srOnly>Transcript</Th>
        </Tr>
      </thead>
      <tbody>
        {data.sessions.map((row) => {
          const wr = row.would_read as Record<string, VerdictPair | Record<string, VerdictPair>>
          const project = wr.project as VerdictPair
          const system = wr.system as VerdictPair
          const ws = String(row.workspace.slug)
          const t = transcripts[row.id]
          return (
            <Fragment key={row.id}>
              <Tr rule="bottom-last" top>
                <Td>
                  <Link className="underline" to={sessionHref(ws, row.id)}>{row.title || 'untitled'}</Link>
                  <div className="text-[11px] text-muted-foreground">
                    {ws} · {relativeAge(row.started_at, undefined, { seconds: false })}
                  </div>
                </Td>
                <Td hideBelow="md" tone="muted">{row.participants.map((p) => p.name).join(', ')}</Td>
                <Td><VerdictCell v={project.now} row={row} /></Td>
                <Td><VerdictCell v={project.if_everyone_agreed} row={row} /></Td>
                <Td hideBelow="sm"><VerdictCell v={system.now} row={row} /></Td>
                <Td hideBelow="sm"><VerdictCell v={system.if_everyone_agreed} row={row} /></Td>
                <Td hideBelow="lg"><VerdictCell v={row.you?.now} row={row} /></Td>
                <Td>
                  <button type="button" className="text-xs underline" onClick={() => void toggle(row)}>
                    {open === row.id ? 'Hide' : 'Transcript'}
                  </button>
                </Td>
              </Tr>
              {open === row.id && (
                <Tr>
                  <Td colSpan={8}>
                    {!t ? (
                      <Empty>Loading…</Empty>
                    ) : typeof t === 'string' ? (
                      <p className="text-xs text-destructive">{t}</p>
                    ) : (
                      <TranscriptView t={t} />
                    )}
                  </Td>
                </Tr>
              )}
            </Fragment>
          )
        })}
      </tbody>
    </Table>
  )
}

function TranscriptView({ t }: { t: PreviewTranscript }): JSX.Element {
  const messages = t.messages as { index: number; role: string; text: string; author?: { name?: string } | null }[]
  return (
    <div className="max-h-96 space-y-1 overflow-y-auto rounded-md border border-border bg-background p-2 text-xs">
      <p className="text-muted-foreground">
        As a learner reads it ({t.view}), secrets scrubbed{t.truncated ? '; cut at the size limit' : ''}.
      </p>
      {messages.map((m) => (
        <div key={m.index} className="whitespace-pre-wrap break-words">
          <span className="font-medium text-foreground">{m.author?.name || m.role}:</span>{' '}
          <span className="text-foreground-secondary">{m.text}</span>
        </div>
      ))}
    </div>
  )
}
