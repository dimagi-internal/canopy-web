import { Fragment, useCallback, useEffect, useState, type JSX, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Button, StatusBadge } from 'canopy-ui/ui'

import {
  getBrainLinks,
  getBrainRuns,
  getBrainState,
  listBrainOutputs,
  runLearnerNow,
  type BrainOutput,
  type BrainRun,
  type BrainState,
  type BrainStoredLink,
} from '@/api/brain'
import { Td, Th, Tr } from '@/components/DataTable'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'

// THE BRAIN: canopy learning as it stands. It renders; it never decides: every count is the server's
// (`apps/brain/state.py`). The routes refuse an agent, a turn's token and an MCP client, and answer only with sessions
// and projects you already read. A canopy admin starts a learner's run here ("Run now"); what it found shows under
// Findings, which act on nothing by themselves.

const CLASSES = ['individual', 'memory', 'project', 'system'] as const

/** Where one session's page is. */
const sessionHref = (workspace: string, id: string) => scopedPath(workspace, `/sessions/${id}`)

export function BrainPage(): JSX.Element {
  const [state, setState] = useState<BrainState | null>(null)
  const [error, setError] = useState('')
  // Bumped when a run starts, so the runs list reads again.
  const [runsKey, setRunsKey] = useState(0)

  useEffect(() => {
    getBrainState().then(setState, (e: unknown) => setError(e instanceof Error ? e.message : 'Could not load'))
  }, [])

  const home = state?.workspaces[0]
  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4 sm:p-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-foreground">Canopy Brain</h1>
          <p className="text-[13px] text-muted-foreground">
            What canopy may learn from, who agreed, what learns, and what it found. Only you see this view of it.
          </p>
        </div>
        <nav aria-label="Related" className="flex flex-wrap gap-3 text-[13px]">
          <Link className="underline" to="/settings/learning">Your learning settings</Link>
          {home && <Link className="underline" to={scopedPath(home, '/network')}>Who works with whom</Link>}
        </nav>
      </header>
      {error ? (
        <p className="text-[13px] text-destructive">{error}</p>
      ) : !state ? (
        <p className="text-[13px] text-muted-foreground">Loading…</p>
      ) : (
        <NowTab state={state} runsKey={runsKey} onStarted={() => setRunsKey((k) => k + 1)} />
      )}
    </div>
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

function NowTab({ state, runsKey, onStarted }: { state: BrainState; runsKey: number; onStarted: () => void }): JSX.Element {
  return (
    <div className="space-y-4">
      <Section title="Rollout">
        <p className="flex flex-wrap items-center gap-2 text-[13px] text-foreground">
          <StatusBadge tone={state.reads_enabled ? 'success' : 'neutral'} dot>
            {state.reads_enabled ? 'Reads on' : 'Reads off'}
          </StatusBadge>
          {state.reads_enabled
            ? 'Learners may read the sessions everyone in them agreed to.'
            : 'No learner reads a session yet. Consent and links work; nothing is learned until BRAIN_READS_ENABLED is on.'}
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

      <StoredLinks state={state} />

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
              {state.canopy_admin && <Th srOnly>Run now</Th>}
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
                {state.canopy_admin && (
                  <Td>
                    <RunNow learner={l} reads={state.reads_enabled} onStarted={onStarted} />
                  </Td>
                )}
              </Tr>
            ))}
          </tbody>
        </Table>
      </Section>

      <Runs key={runsKey} state={state} />
      <Findings />

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
  const reload = useCallback(() => void more(0), [more])
  return { rows, error, done, loadMore: () => void more(next), reload }
}

function StoredLinks({ state }: { state: BrainState }): JSX.Element {
  const { rows, error, done, loadMore } = usePaged<BrainStoredLink>(getBrainLinks, 'links')
  return (
    <Section title="Stored links" note="Which project each session you can read is tied to, and how sure canopy is.">
      {state.links.length === 0 ? (
        <Empty>No stored links on sessions you read.</Empty>
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

/** How often the runs list reads again while a run is still open. */
const RUN_POLL_MS = 15_000

function Runs({ state }: { state: BrainState }): JSX.Element {
  const { rows, error, done, loadMore, reload } = usePaged<BrainRun>(getBrainRuns, 'runs')
  const running = rows.some((r) => r.state === 'open')
  useEffect(() => {
    if (!running) return
    const t = window.setInterval(reload, RUN_POLL_MS)
    return () => window.clearInterval(t)
  }, [running, reload])
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

type Learner = BrainState['learners'][number]

/** "Run now": one run of a learner, started by a canopy admin. The server says why when it cannot start. */
function RunNow({ learner, reads, onStarted }: { learner: Learner; reads: boolean; onStarted: () => void }): JSX.Element {
  const [project, setProject] = useState('')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const needsProject = learner.purpose_class === 'project'
  if (learner.purpose_class === 'individual' || learner.purpose_class === 'memory') {
    return <span className="text-xs text-muted-foreground">per person</span>
  }
  const go = async () => {
    setBusy(true)
    setNote('')
    try {
      const run = await runLearnerNow(learner.slug, project)
      setNote(`Run ${run.id} started`)
      onStarted()
    } catch (e) {
      setNote(e instanceof Error ? e.message : 'Could not start')
    } finally {
      setBusy(false)
    }
  }
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      {needsProject && (
        <input
          aria-label={`Project for ${learner.slug}`}
          placeholder="project handle"
          value={project}
          onChange={(e) => setProject(e.target.value)}
          className="w-28 rounded-md border border-input bg-background px-1.5 py-0.5 text-xs text-foreground"
        />
      )}
      <Button size="sm" variant="outline" disabled={busy || (needsProject && !project.trim())}
              title={reads ? undefined : 'Learning reads are switched off; the server will say so'}
              onClick={() => void go()}>
        {busy ? 'Starting…' : 'Run now'}
      </Button>
      {note && <span role="status" className="text-xs text-muted-foreground">{note}</span>}
    </span>
  )
}

/** A finding's own words: the plain fields of its body, in order. */
function bodyFields(body: Record<string, unknown>): [string, string][] {
  return Object.entries(body)
    .filter(([, v]) => v !== null && v !== '' && (typeof v !== 'object' || Array.isArray(v)))
    .map(([k, v]) => [k.replace(/_/g, ' '), Array.isArray(v) ? v.map(String).join(', ') : String(v)])
}

function Findings(): JSX.Element {
  const [rows, setRows] = useState<BrainOutput[] | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    listBrainOutputs().then(setRows, (e: unknown) => setError(e instanceof Error ? e.message : 'Could not load'))
  }, [])
  return (
    <Section
      title="Findings"
      note="What learning produced that you may see, for the audience its class allows. A finding acts on nothing by itself: deciding what to do with it is yours."
    >
      {error ? (
        <p className="text-xs text-destructive">{error}</p>
      ) : !rows ? (
        <Empty>Loading…</Empty>
      ) : rows.length === 0 ? (
        <Empty>None yet.</Empty>
      ) : (
        <ul className="divide-y divide-border">
          {rows.map((o) => (
            <li key={o.id} className="space-y-1 py-2 text-[13px]">
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="font-medium text-foreground">{o.title || o.kind}</span>
                <StatusBadge tone="neutral">{o.kind}</StatusBadge>
                <span className="text-xs text-muted-foreground">
                  {o.learner} · {o.subject_type}{o.subject ? ` ${o.subject}` : ''} ·{' '}
                  {relativeAge(o.created_at, undefined, { seconds: false })}
                </span>
              </div>
              {bodyFields(o.body as Record<string, unknown>).length > 0 && (
                <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-xs">
                  {bodyFields(o.body as Record<string, unknown>).map(([k, v]) => (
                    <Fragment key={k}>
                      <dt className="text-muted-foreground">{k}</dt>
                      <dd className="whitespace-pre-wrap break-words text-foreground">{v}</dd>
                    </Fragment>
                  ))}
                </dl>
              )}
              <p className="text-xs text-muted-foreground">
                From{' '}
                {(o.sessions ?? []).map((s, i) => (
                  <Fragment key={s.session_id}>
                    {i > 0 && ', '}
                    <Link className="underline" to={sessionHref(s.workspace, s.session_id)}>{s.title || 'untitled'}</Link>
                  </Fragment>
                ))}
                {(o.sessions ?? []).length === 0 && (o.hidden_sessions ?? 0) === 0 && 'no session'}
                {(o.hidden_sessions ?? 0) > 0 &&
                  `${(o.sessions ?? []).length > 0 ? ' and ' : ''}${o.hidden_sessions} session${o.hidden_sessions === 1 ? '' : 's'} you do not read`}
              </p>
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}
