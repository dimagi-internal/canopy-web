import { Fragment, useCallback, useEffect, useState, type JSX, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Button, StatusBadge } from 'canopy-ui/ui'

import {
  configureLearner,
  getBrainLinks,
  getBrainRuns,
  getBrainState,
  listBrainOutputs,
  runLearnerNow,
  setLearningReads,
  type BrainOutput,
  type BrainRun,
  type BrainState,
  type BrainStoredLink,
  type LearningReads,
} from '@/api/brain'
import { Td, Th, Tr } from '@/components/DataTable'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'
import { HealthViews } from './health/HealthViews'

// THE BRAIN of one workspace (/w/:workspace/brain): canopy learning in it and the workspaces below it, as the
// viewer's role there allows. Its owners and admins see the subtree's counts, links, runs and findings; a member sees
// their own. It renders; it never decides: every count and every refusal is the server's (`apps/brain/state.py`), and
// every read is pinned to this workspace. Its owners and admins (or an ancestor's, or a canopy admin) turn "Learning
// reads" on or off here, enable and configure the learners whose operator lives here, and start a learner's run
// ("Run now"), reading only this subtree; what it found shows under Findings, which act on nothing by themselves.

const CLASSES = ['individual', 'memory', 'project', 'system'] as const

/** Where one session's page is. */
const sessionHref = (workspace: string, id: string) => scopedPath(workspace, `/sessions/${id}`)

export function BrainPage(): JSX.Element {
  const { workspace = '' } = useParams()
  const [state, setState] = useState<BrainState | null>(null)
  const [error, setError] = useState('')
  // Bumped when a run starts, so the runs list reads again.
  const [runsKey, setRunsKey] = useState(0)
  // Bumped after a change here (learning reads, a learner), so the state reads again.
  const [stateKey, setStateKey] = useState(0)

  useEffect(() => {
    getBrainState(workspace).then(setState, (e: unknown) => setError(e instanceof Error ? e.message : 'Could not load'))
  }, [workspace, stateKey])
  useEffect(() => {
    setState(null)
    setError('')
  }, [workspace])

  return (
    <div className="space-y-4">
      <HealthViews title="Learning" />
      <nav aria-label="Related" className="flex flex-wrap gap-3 text-[13px]">
        <Link className="underline" to="/settings/learning">Your learning settings</Link>
      </nav>
      {error ? (
        <p className="text-[13px] text-destructive">{error}</p>
      ) : !state ? (
        <p className="text-[13px] text-muted-foreground">Loading…</p>
      ) : (
        <NowTab key={workspace} ws={workspace} state={state} runsKey={runsKey}
                onStarted={() => setRunsKey((k) => k + 1)} onChanged={() => setStateKey((k) => k + 1)} />
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

function NowTab({ ws, state, runsKey, onStarted, onChanged }: {
  ws: string; state: BrainState; runsKey: number; onStarted: () => void; onChanged: () => void
}): JSX.Element {
  const readsOn = state.learning_reads?.on ?? false
  // The learner whose settings are open, by slug.
  const [editing, setEditing] = useState('')
  return (
    <div className="space-y-4">
      {state.learning_reads && <LearningReadsSection ws={ws} reads={state.learning_reads} onChanged={onChanged} />}

      <Section
        title="Consent"
        note={
          <>
            Members who agreed to each class, per workspace: counts, never names; a count under{' '}
            {state.consent.workspaces[0]?.small ?? 3} shows as &lt;{state.consent.workspaces[0]?.small ?? 3}. You agreed to{' '}
            {state.consent.mine.length ? state.consent.mine.join(', ') : 'nothing yet'}.
          </>
        }
      >
        {state.consent.workspaces.length === 0 ? (
          <Empty>Counts by workspace are for its owners and admins.</Empty>
        ) : (
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
                  <Td numeric align="right">{w.agreed_any ?? `<${w.small}`}</Td>
                  {CLASSES.map((c) => (
                    <Td key={c} numeric align="right">{w.agreed[c] ?? `<${w.small}`}</Td>
                  ))}
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
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

      <StoredLinks ws={ws} state={state} />

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
              {state.can_manage && <Th srOnly>Manage</Th>}
            </Tr>
          </thead>
          <tbody>
            {state.learners.map((l) => (
              <Fragment key={l.slug}>
              <Tr rule="bottom-last">
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
                {state.can_manage && (
                  <Td>
                    <span className="flex flex-wrap items-center gap-1.5">
                      <RunNow ws={ws} learner={l} reads={readsOn} onStarted={onStarted} />
                      {l.configurable && (
                        <Button size="sm" variant="ghost" onClick={() => setEditing(editing === l.slug ? '' : l.slug)}>
                          {editing === l.slug ? 'Close' : 'Configure'}
                        </Button>
                      )}
                    </span>
                  </Td>
                )}
              </Tr>
              {editing === l.slug && (
                <Tr rule="bottom-last">
                  <Td colSpan={8}>
                    <LearnerForm ws={ws} learner={l} onSaved={() => { setEditing(''); onChanged() }} />
                  </Td>
                </Tr>
              )}
              </Fragment>
            ))}
          </tbody>
        </Table>
      </Section>

      <Runs key={runsKey} ws={ws} state={state} />
      <Findings ws={ws} />

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

function StoredLinks({ ws, state }: { ws: string; state: BrainState }): JSX.Element {
  const load = useCallback((before: number) => getBrainLinks(ws, before), [ws])
  const { rows, error, done, loadMore } = usePaged<BrainStoredLink>(load, 'links')
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

function Runs({ ws, state }: { ws: string; state: BrainState }): JSX.Element {
  const load = useCallback((before: number) => getBrainRuns(ws, before), [ws])
  const { rows, error, done, loadMore, reload } = usePaged<BrainRun>(load, 'runs')
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

/** "Run now": one run of a learner for this workspace's subtree, started by its owners or admins (or a canopy admin),
 *  who then answer for it. The server says why when it cannot start. */
function RunNow({ ws, learner, reads, onStarted }: {
  ws: string; learner: Learner; reads: boolean; onStarted: () => void
}): JSX.Element {
  const [project, setProject] = useState('')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const needsProject = learner.purpose_class === 'project'
  if (learner.purpose_class === 'individual' || learner.purpose_class === 'memory') {
    return <span className="text-xs text-muted-foreground">per person</span>
  }
  if (!learner.runnable) return <span className="text-xs text-muted-foreground">built in</span>
  const go = async () => {
    setBusy(true)
    setNote('')
    try {
      const run = await runLearnerNow(ws, learner.slug, project)
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
              title={reads ? undefined : 'Learning reads are off here; the server will say so'}
              onClick={() => void go()}>
        {busy ? 'Starting…' : 'Run now'}
      </Button>
      {note && <span role="status" className="text-xs text-muted-foreground">{note}</span>}
    </span>
  )
}

const onOff = (v: boolean | null | undefined) => (v == null ? 'inherit' : v ? 'on' : 'off')

/** "Learning reads": whether learners may read this workspace's sessions at all. Its owners and admins change it; every
 *  change is listed, newest first. */
function LearningReadsSection({ ws, reads, onChanged }: {
  ws: string; reads: LearningReads; onChanged: () => void
}): JSX.Element {
  const [choice, setChoice] = useState(onOff(reads.own))
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const save = async () => {
    setBusy(true)
    setNote('')
    try {
      await setLearningReads(ws, choice === 'inherit' ? null : choice === 'on')
      onChanged()
    } catch (e) {
      setNote(e instanceof Error ? e.message : 'Could not save')
    } finally {
      setBusy(false)
    }
  }
  const where = reads.own != null
    ? 'set here'
    : reads.source ? `inherited from ${reads.source}` : 'not set here or above, so off'
  return (
    <Section
      title="Learning reads"
      note="Whether learners may read this workspace's sessions (and the ones below it that do not set their own). On, a session is still read only when everyone in it agreed."
    >
      <p className="flex flex-wrap items-center gap-2 text-[13px] text-foreground">
        <StatusBadge tone={reads.on ? 'success' : 'neutral'} dot>{reads.on ? 'On' : 'Off'}</StatusBadge>
        <span className="text-muted-foreground">{where}</span>
      </p>
      {reads.can_edit && (
        <div className="flex flex-wrap items-center gap-1.5 text-[13px]">
          <label htmlFor="learning-reads" className="text-muted-foreground">Learning reads in {ws}</label>
          <select
            id="learning-reads"
            value={choice}
            onChange={(e) => setChoice(e.target.value)}
            className="rounded-md border border-input bg-background px-1.5 py-0.5 text-xs text-foreground"
          >
            <option value="on">On</option>
            <option value="off">Off</option>
            <option value="inherit">Inherit</option>
          </select>
          <Button size="sm" variant="outline" disabled={busy || choice === onOff(reads.own)} onClick={() => void save()}>
            {busy ? 'Saving…' : 'Save'}
          </Button>
          {note && <span role="status" className="text-xs text-destructive">{note}</span>}
        </div>
      )}
      {(reads.changes ?? []).length > 0 && (
        <Table>
          <thead>
            <Tr head="label">
              <Th>When</Th>
              <Th>Who</Th>
              <Th>Setting</Th>
              <Th>In effect</Th>
            </Tr>
          </thead>
          <tbody>
            {(reads.changes ?? []).map((c) => (
              <Tr key={`${c.at}-${c.by}`} rule="bottom-last">
                <Td tone="muted" nowrap>{relativeAge(c.at, undefined, { seconds: false })}</Td>
                <Td>{c.by || 'someone'}</Td>
                <Td>{onOff(c.before)} → {onOff(c.after)}</Td>
                <Td>{c.was_on ? 'on' : 'off'} → {c.now_on ? 'on' : 'off'}</Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
    </Section>
  )
}

const SCHEDULES = ['', 'hourly', 'daily', 'weekly'] as const

/** A learner's settings a workspace's owners and admins may change: whether it is on, its operator agent (of this
 *  workspace or below), the plugin command a run starts with, and its schedule. The server refuses the rest. */
function LearnerForm({ ws, learner, onSaved }: { ws: string; learner: Learner; onSaved: () => void }): JSX.Element {
  const [enabled, setEnabled] = useState(learner.enabled)
  const [operator, setOperator] = useState(learner.operator)
  const [entry, setEntry] = useState(learner.entry)
  const [schedule, setSchedule] = useState(learner.schedule)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const field = 'rounded-md border border-input bg-background px-1.5 py-0.5 text-xs text-foreground'
  const save = async () => {
    setBusy(true)
    setNote('')
    try {
      await configureLearner(ws, learner.slug, {
        operator: operator.trim(),
        entry: entry.trim(),
        schedule: schedule as (typeof SCHEDULES)[number],
        enabled,
      })
      onSaved()
    } catch (e) {
      setNote(e instanceof Error ? e.message : 'Could not save')
    } finally {
      setBusy(false)
    }
  }
  return (
    <form
      aria-label={`Configure ${learner.slug}`}
      className="flex flex-wrap items-center gap-2 text-xs"
      onSubmit={(e) => { e.preventDefault(); void save() }}
    >
      <label className="flex items-center gap-1">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Enabled
      </label>
      <label className="flex items-center gap-1">
        Operator
        <input aria-label="Operator agent" placeholder="agent slug" value={operator}
               onChange={(e) => setOperator(e.target.value)} className={`w-24 ${field}`} />
      </label>
      <label className="flex items-center gap-1">
        Entry
        <input aria-label="Entry command" placeholder="/plugin:skill" value={entry}
               onChange={(e) => setEntry(e.target.value)} className={`w-44 ${field}`} />
      </label>
      <label className="flex items-center gap-1">
        Schedule
        <select aria-label="Schedule" value={schedule} onChange={(e) => setSchedule(e.target.value)} className={field}>
          {SCHEDULES.map((s) => <option key={s} value={s}>{s || 'by hand'}</option>)}
        </select>
      </label>
      <Button size="sm" variant="outline" type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save'}</Button>
      {note && <span role="status" className="text-destructive">{note}</span>}
    </form>
  )
}

/** A finding's own words: the plain fields of its body, in order. */
function bodyFields(body: Record<string, unknown>): [string, string][] {
  return Object.entries(body)
    .filter(([, v]) => v !== null && v !== '' && (typeof v !== 'object' || Array.isArray(v)))
    .map(([k, v]) => [k.replace(/_/g, ' '), Array.isArray(v) ? v.map(String).join(', ') : String(v)])
}

function Findings({ ws }: { ws: string }): JSX.Element {
  const [rows, setRows] = useState<BrainOutput[] | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    listBrainOutputs(ws).then(setRows, (e: unknown) => setError(e instanceof Error ? e.message : 'Could not load'))
  }, [ws])
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
