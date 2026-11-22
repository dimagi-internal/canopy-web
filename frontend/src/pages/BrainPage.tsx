import { Fragment, useCallback, useEffect, useState, type JSX, type ReactNode } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { Button, StatusBadge, Tabs, TabsList, TabsTrigger } from 'canopy-ui/ui'

import {
  acknowledgeFlag,
  addTarget,
  configureLearner,
  getBrainLinks,
  getBrainRuns,
  getBrainState,
  removeTarget,
  runTargetNow,
  setLearningReads,
  setTargetBudget,
  whyNotRead,
  type BrainFlag,
  type BrainWhy,
  type BrainRun,
  type BrainState,
  type BrainStoredLink,
  type BrainTarget,
  type BrainTargetBudgetIn,
  type LearningReads,
} from '@/api/brain'
import { Td, Th, Tr } from '@/components/DataTable'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'
import { Findings } from './BrainFindings'
import { HealthViews } from './health/HealthViews'

// THE BRAIN of one workspace (/w/:workspace/brain): canopy learning in it and the workspaces below it, as the
// viewer's role there allows. Its owners and admins see the subtree's counts, links, runs and findings; a member sees
// their own. It renders; it never decides: every count and every refusal is the server's (`apps/brain/state.py`), and
// every read is pinned to this workspace. Its owners and admins (or an ancestor's, or a canopy admin) turn "Learning
// reads" on or off here, enable and configure the learners whose operator lives here, and start a learner's run
// ("Run now"), reading only this subtree. Two tabs: Findings (the default; what learning found, for whoever acts on it,
// `BrainFindings.tsx`) and "Setup and audit" (`?tab=setup`: everything that makes learning run and shows what it read).

const CLASSES = ['individual', 'memory', 'project', 'system'] as const

/** Where one session's page is. */
const sessionHref = (workspace: string, id: string) => scopedPath(workspace, `/sessions/${id}`)

export function BrainPage(): JSX.Element {
  const { workspace = '' } = useParams()
  const [search, setSearch] = useSearchParams()
  const tab = search.get('tab') === 'setup' ? 'setup' : 'findings'
  return (
    <div className="space-y-4">
      <HealthViews title="Learning" />
      <nav aria-label="Related" className="flex flex-wrap gap-3 text-[13px]">
        <Link className="underline" to="/settings/learning">Your learning settings</Link>
      </nav>
      <Tabs value={tab} onValueChange={(next) => setSearch(next === 'setup' ? { tab: 'setup' } : {})}>
        <TabsList>
          <TabsTrigger value="findings">Findings</TabsTrigger>
          <TabsTrigger value="setup">Setup and audit</TabsTrigger>
        </TabsList>
      </Tabs>
      {tab === 'setup' ? <SetupTab key={workspace} ws={workspace} /> : <Findings key={workspace} ws={workspace} />}
    </div>
  )
}

/** Everything that makes learning run and shows what it read: the workspace's brain state, its settings and runs. */
function SetupTab({ ws }: { ws: string }): JSX.Element {
  const [state, setState] = useState<BrainState | null>(null)
  const [error, setError] = useState('')
  // Bumped when a run starts, so the runs list reads again.
  const [runsKey, setRunsKey] = useState(0)
  // Bumped after a change here (learning reads, a learner), so the state reads again.
  const [stateKey, setStateKey] = useState(0)

  useEffect(() => {
    getBrainState(ws).then(setState, (e: unknown) => setError(e instanceof Error ? e.message : 'Could not load'))
  }, [ws, stateKey])

  return error ? (
    <p className="text-[13px] text-destructive">{error}</p>
  ) : !state ? (
    <p className="text-[13px] text-muted-foreground">Loading…</p>
  ) : (
    <NowTab ws={ws} state={state} runsKey={runsKey}
            onStarted={() => setRunsKey((k) => k + 1)} onChanged={() => setStateKey((k) => k + 1)} />
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
              <Th align="right">Targets</Th>
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
                <Td numeric align="right">
                  {l.runnable ? (l.targets ?? 0) : <span className="text-xs text-muted-foreground">
                    {l.purpose_class === 'individual' || l.purpose_class === 'memory' ? 'per person' : 'built in'}
                  </span>}
                </Td>
                <Td hideBelow="md" tone="muted">{l.operator || '—'}</Td>
                <Td tone="muted">{l.accountable || 'nobody yet'}</Td>
                {state.can_manage && (
                  <Td>
                    {l.configurable && (
                      <Button size="sm" variant="ghost" onClick={() => setEditing(editing === l.slug ? '' : l.slug)}>
                        {editing === l.slug ? 'Close' : 'Configure'}
                      </Button>
                    )}
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

      <Targets ws={ws} state={state} reads={readsOn} onStarted={onStarted} onChanged={onChanged} />

      <Runs key={runsKey} ws={ws} state={state} />
      <WhyNotRead ws={ws} state={state} />
      <Flags ws={ws} flags={state.flags ?? []} onChanged={onChanged} />

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
                  {r.context && <RunContext context={r.context} />}
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

/** What a run was told when it started (its context document, `version` and all), without the ids of the
 *  sessions due: the server leaves those out, since you may not read them. */
function RunContext({ context }: { context: NonNullable<BrainRun['context']> }): JSX.Element {
  const t = context.target
  const rows: [string, string][] = [
    ['version', String(context.version)],
    ['class', context.purpose_class],
    ['target', t ? (t.kind === 'project' ? `${t.workspace}/${t.project}` : t.workspace) : context.scope || 'none'],
    ['accountable', context.accountable?.email ?? 'nobody'],
    ['sessions due', `${context.sessions_due}${context.lookback_days ? ` (active in the last ${context.lookback_days} days)` : ''}`],
    ['open findings told', String(context.open_findings_total ?? 0)],
    ['budget', `${context.budget.max_sources_per_run} sessions, ${context.budget.max_outputs_per_run} outputs a run`],
    ['entry', context.entry || '-'],
    ['emits', (context.output_kinds ?? []).join(', ') || 'nothing'],
  ]
  return (
    <details className="text-xs">
      <summary className="cursor-pointer text-muted-foreground">context</summary>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5">
        {rows.map(([k, v]) => (
          <Fragment key={k}>
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="text-foreground">{v}</dd>
          </Fragment>
        ))}
      </dl>
    </details>
  )
}

type Learner = BrainState['learners'][number]

/** Where learners run: each target a workspace (with the ones below it) or a workspace project, on a schedule of its own
 *  (a normal canopy schedule on the operator agent, listed beside its other schedules). Its owners and admins (or an
 *  ancestor's) add targets here, run one now (its schedule's own Run now) or take one away. A learner with no target
 *  does not run. */
function Targets({ ws, state, reads, onStarted, onChanged }: {
  ws: string; state: BrainState; reads: boolean; onStarted: () => void; onChanged: () => void
}): JSX.Element {
  const aimable = state.learners.filter((l) => l.runnable)
  const [learner, setLearner] = useState(aimable[0]?.slug ?? '')
  const [where, setWhere] = useState(ws)
  const [project, setProject] = useState('')
  const [cron, setCron] = useState('0 6 * * *')
  const [tz, setTz] = useState('UTC')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const field = 'rounded-md border border-input bg-background px-1.5 py-0.5 text-xs text-foreground'
  const act = async <T,>(what: () => Promise<T>, done: (r: T) => string, after: () => void) => {
    setBusy(true)
    setNote('')
    try {
      setNote(done(await what()))
      after()
    } catch (e) {
      setNote(e instanceof Error ? e.message : 'Could not do that')
    } finally {
      setBusy(false)
    }
  }
  const targets: readonly BrainTarget[] = state.targets ?? []
  // The target whose budget form is open, by id (0: none).
  const [budgetFor, setBudgetFor] = useState(0)
  const editing = targets.find((t) => t.id === budgetFor)
  return (
    <Section
      title="Targets"
      note="Where each learner runs, on its own schedule. A learner with no target does not run; a run reads only its target's sessions that everyone in them agreed to."
    >
      {targets.length === 0 ? (
        <Empty>No learner is aimed here or below.</Empty>
      ) : (
        <Table>
          <thead>
            <Tr head="label">
              <Th>Learner</Th>
              <Th>Target</Th>
              <Th hideBelow="sm">Schedule</Th>
              <Th hideBelow="md">Agent</Th>
              <Th>Last run</Th>
              <Th hideBelow="md">Budget</Th>
              <Th srOnly>Manage</Th>
            </Tr>
          </thead>
          <tbody>
            {targets.map((t) => (
              <Tr key={t.id} rule="bottom-last">
                <Td>{t.learner}</Td>
                <Td>{t.label}</Td>
                <Td hideBelow="sm" tone="muted">
                  <span className="font-mono">{t.cron}</span> {t.timezone}{t.enabled ? '' : ' (paused)'}
                </Td>
                <Td hideBelow="md" tone="muted">{t.agent}</Td>
                <Td tone="muted">
                  {t.last_run ? `${t.last_run.state}, ${relativeAge(t.last_run.created_at, undefined, { seconds: false })}` : 'never'}
                </Td>
                <Td hideBelow="md" tone="muted">{t.budget && budgetLine(t.budget)}</Td>
                <Td>
                  {t.can_manage && (
                    <span className="flex flex-wrap items-center gap-1.5">
                      <Button size="sm" variant="ghost" disabled={busy}
                              onClick={() => setBudgetFor(budgetFor === t.id ? 0 : t.id)}>
                        Budget…
                      </Button>
                      <Button size="sm" variant="outline" disabled={busy}
                              title={reads ? undefined : 'Learning reads are off here; the server will say so'}
                              onClick={() => void act(() => runTargetNow(ws, t.id), (r) => `Run ${r.id} started`, onStarted)}>
                        Run now
                      </Button>
                      <Button size="sm" variant="ghost" disabled={busy}
                              onClick={() => void act(() => removeTarget(ws, t.id), () => `Took ${t.label} away`, onChanged)}>
                        Remove
                      </Button>
                    </span>
                  )}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
      {editing?.budget && (
        <BudgetForm key={editing.id} target={editing} busy={busy}
                    onSave={(body) => void act(() => setTargetBudget(ws, editing.id, body), () => 'Budget saved', () => {
                      setBudgetFor(0)
                      onChanged()
                    })} />
      )}
      {state.can_manage && aimable.length > 0 && (
        <form
          aria-label="Add a target"
          className="flex flex-wrap items-center gap-2 text-xs"
          onSubmit={(e) => {
            e.preventDefault()
            void act(() => addTarget(ws, { learner, workspace: where.trim(), project: project.trim(), cron: cron.trim(),
                                             timezone: tz.trim() }), () => 'Target added', onChanged)
          }}
        >
          <select aria-label="Learner" value={learner} onChange={(e) => setLearner(e.target.value)} className={field}>
            {aimable.map((l) => <option key={l.slug} value={l.slug}>{l.slug}</option>)}
          </select>
          <input aria-label="Workspace" placeholder="workspace" value={where} onChange={(e) => setWhere(e.target.value)}
                 className={`w-24 ${field}`} />
          <input aria-label="Project" placeholder="project (optional)" value={project}
                 onChange={(e) => setProject(e.target.value)} className={`w-32 ${field}`} />
          <input aria-label="Cron" value={cron} onChange={(e) => setCron(e.target.value)} className={`w-24 font-mono ${field}`} />
          <input aria-label="Time zone" value={tz} onChange={(e) => setTz(e.target.value)} className={`w-32 ${field}`} />
          <Button size="sm" variant="outline" type="submit" disabled={busy || !learner}>Add target</Button>
        </form>
      )}
      {note && <p role="status" className="text-xs text-muted-foreground">{note}</p>}
    </Section>
  )
}

type TargetBudget = NonNullable<BrainTarget['budget']>

/** A target's budget and what it spent today (UTC), in one line. */
function budgetLine(b: TargetBudget): string {
  const tokens = b.max_tokens_per_day == null ? `${b.today.tokens} tokens` : `${b.today.tokens}/${b.max_tokens_per_day} tokens`
  const window = b.lookback_days ? ` from the last ${b.lookback_days} days` : ''
  return `≤${b.max_sources_per_run} sessions${window}, ≤${b.max_outputs_per_run} outputs a run · today ${b.today.runs}/${b.max_runs_per_day} runs, ` +
    `${b.today.outputs}/${b.max_outputs_per_day} outputs, ${tokens} · findings fade after ${b.fade_days ?? 21} days unseen`
}

/** Change one target's budget. A run stops cleanly at a cap; a daily cap stops the next fire. */
function BudgetForm({ target, busy, onSave }: {
  target: BrainTarget; busy: boolean; onSave: (body: BrainTargetBudgetIn) => void
}): JSX.Element {
  const b = target.budget as TargetBudget
  const [v, setV] = useState({
    max_sources_per_run: String(b.max_sources_per_run), max_outputs_per_run: String(b.max_outputs_per_run),
    max_runs_per_day: String(b.max_runs_per_day), max_outputs_per_day: String(b.max_outputs_per_day),
    max_tokens_per_day: b.max_tokens_per_day == null ? '' : String(b.max_tokens_per_day),
    lookback_days: String(b.lookback_days ?? ''), fade_days: String(b.fade_days ?? 21),
  })
  const field = 'w-20 rounded-md border border-input bg-background px-1.5 py-0.5 text-xs text-foreground'
  const labels: [keyof typeof v, string][] = [
    ['max_sources_per_run', 'sessions a run'], ['max_outputs_per_run', 'outputs a run'],
    ['max_runs_per_day', 'runs a day'], ['max_outputs_per_day', 'outputs a day'],
    ['max_tokens_per_day', 'tokens a day (empty: no cap)'],
    ['lookback_days', 'days back a run reads'], ['fade_days', 'days before an unseen finding fades'],
  ]
  return (
    <form
      aria-label={`Budget of ${target.label}`}
      className="flex flex-wrap items-center gap-2 text-xs"
      onSubmit={(e) => {
        e.preventDefault()
        const tokens = v.max_tokens_per_day.trim()
        onSave({
          max_sources_per_run: Number(v.max_sources_per_run), max_outputs_per_run: Number(v.max_outputs_per_run),
          max_runs_per_day: Number(v.max_runs_per_day), max_outputs_per_day: Number(v.max_outputs_per_day),
          ...(tokens ? { max_tokens_per_day: Number(tokens) } : {}), clear_tokens: !tokens,
          ...(v.lookback_days.trim() ? { lookback_days: Number(v.lookback_days) } : {}),
          ...(v.fade_days.trim() ? { fade_days: Number(v.fade_days) } : {}),
        })
      }}
    >
      <span className="text-muted-foreground">Budget of {target.label}:</span>
      {labels.map(([k, label]) => (
        <label key={k} className="flex items-center gap-1">
          <input aria-label={label} inputMode="numeric" value={v[k]} className={field}
                 onChange={(e) => setV({ ...v, [k]: e.target.value })} />
          <span className="text-muted-foreground">{label}</span>
        </label>
      ))}
      <Button size="sm" variant="outline" type="submit" disabled={busy}>Save budget</Button>
    </form>
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

/** A learner's settings a workspace's owners and admins may change: whether it is on, its operator agent (of this
 *  workspace or below) and the plugin command a run starts with. Where and when it runs is its targets. The server refuses
 *  the rest. */
function LearnerForm({ ws, learner, onSaved }: { ws: string; learner: Learner; onSaved: () => void }): JSX.Element {
  const [enabled, setEnabled] = useState(learner.enabled)
  const [operator, setOperator] = useState(learner.operator)
  const [entry, setEntry] = useState(learner.entry)
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
      <Button size="sm" variant="outline" type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save'}</Button>
      {note && <span role="status" className="text-destructive">{note}</span>}
    </form>
  )
}

const flagWhat = (f: BrainFlag) =>
  f.kind === 'github.pull' ? 'Pull request' : f.kind === 'github.issue' ? 'Issue' : f.kind === 'link' ? 'Link'
    : f.kind.split('.').pop() ?? f.kind

/** What a learning run made that a removal could not undo: a sent email, a published update, a pull request. Its owner
 *  (the person the run answered to) and the workspace's managers see it here, decide what to do with it, and
 *  acknowledge it, which closes the alert raised to the owner. Shown only when there is one. */
function Flags({ ws, flags, onChanged }: { ws: string; flags: readonly BrainFlag[]; onChanged: () => void }): JSX.Element | null {
  const [note, setNote] = useState('')
  if (flags.length === 0) return null
  const ack = async (id: number) => {
    setNote('')
    try {
      await acknowledgeFlag(ws, id)
      onChanged()
    } catch (e) {
      setNote(e instanceof Error ? e.message : 'Could not acknowledge it')
    }
  }
  return (
    <Section
      title="Could not be undone"
      note="A learning run made these from sessions that were later removed from learning. canopy withdrew what it could; these it cannot take back. Decide whether each should stand, then acknowledge it."
    >
      <Table>
        <thead>
          <Tr head="label">
            <Th>What</Th>
            <Th>Where</Th>
            <Th hideBelow="sm">Run</Th>
            <Th hideBelow="md">Owner</Th>
            <Th hideBelow="md">Why</Th>
            <Th srOnly>Acknowledge</Th>
          </Tr>
        </thead>
        <tbody>
          {flags.map((f) => (
            <Tr key={f.id} rule="bottom-last">
              <Td>{flagWhat(f)}</Td>
              <Td>
                {f.ref.startsWith('http') ? <a className="underline" href={f.ref} target="_blank" rel="noreferrer">{f.ref}</a>
                  : <span className="font-mono">#{f.ref}</span>}
              </Td>
              <Td hideBelow="sm" tone="muted">{f.learner} {f.run}</Td>
              <Td hideBelow="md" tone="muted">{f.owner || '—'}</Td>
              <Td hideBelow="md" tone="muted">{f.reason}</Td>
              <Td>
                {f.acknowledged ? <StatusBadge tone="neutral">acknowledged</StatusBadge> : (
                  <Button size="sm" variant="outline" onClick={() => void ack(f.id)}>Acknowledge</Button>
                )}
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
      {note && <p role="status" className="text-xs text-destructive">{note}</p>}
    </Section>
  )
}

/** "Why can't this session be read?": for a session you are in or read yourself, whether a learner may read it and,
 *  if not, why, by the same rule every learning read asks. A reason about a person says "you" or "someone in it". */
function WhyNotRead({ ws, state }: { ws: string; state: BrainState }): JSX.Element {
  const [session, setSession] = useState('')
  const [learner, setLearner] = useState('project-readers')
  const [got, setGot] = useState<BrainWhy | null>(null)
  const [error, setError] = useState('')
  const field = 'rounded-md border border-input bg-background px-1.5 py-0.5 text-xs text-foreground'
  // A session's id, or the link to its page.
  const id = (session.trim().match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i) ?? [''])[0]
  return (
    <Section title="Why is a session not read?"
             note="For a session you are in or read yourself: whether a learner may read it, and why not. Nobody else is named.">
      <form
        aria-label="Why is a session not read"
        className="flex flex-wrap items-center gap-2 text-xs"
        onSubmit={(e) => {
          e.preventDefault()
          setError('')
          setGot(null)
          whyNotRead(ws, id, learner).then(setGot, (err: unknown) => setError(err instanceof Error ? err.message : 'Could not look that up'))
        }}
      >
        <input aria-label="Session" placeholder="session id or link" value={session}
               onChange={(e) => setSession(e.target.value)} className={`w-80 ${field}`} />
        <select aria-label="Learner" value={learner} onChange={(e) => setLearner(e.target.value)} className={field}>
          {state.learners.map((l) => <option key={l.slug} value={l.slug}>{l.slug}</option>)}
        </select>
        <Button size="sm" variant="outline" type="submit" disabled={!id}>Look up</Button>
      </form>
      {error && <p className="text-xs text-destructive">{error}</p>}
      {got && (
        <p role="status" className="text-xs">
          {got.eligible ? `${got.learner} may read it.` : `${got.learner} may not read it: ${got.explanation}.`}
        </p>
      )}
    </Section>
  )
}
