// ONE TASK — its page and its peek are this one component, so the side panel
// is the page rendered beside a list, never a second design. In order: the
// properties (edited in place), what it asks you (with its decision), the
// people's own words, the plan, its subtasks, what it relates to, the agent's
// session on it, and its whole history with the reply box.
import { useCallback, useEffect, useMemo, useState, type JSX, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Bot, ExternalLink, Link2, X } from 'lucide-react'
import { Button, StatusBadge } from 'canopy-ui/ui'

import {
  actOn,
  addRelation,
  createLabel,
  createTask,
  getTask,
  listMilestones,
  removeRelation,
  updateTask,
  type Milestone,
  type TaskActionName,
  type TaskDetail,
  type TaskEvent,
  type TaskPatch,
} from '@/api/work'
import { Markdown } from '@/components/Markdown'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'
import { TASK_RESOURCE, useResource } from '@/widget/useResource'

import { AssigneePicker, DueInput, LabelChips, LabelPicker, PriorityPicker, ProjectPicker, StatusIcon, StatusPicker } from './bits'
import {
  STATUS_CATEGORY,
  displayAgent,
  displayPerson,
  isOpen,
  priorityLabel,
  statusName,
} from './meta'
import { TaskTable } from './TaskTable'
import { invalidateDirectory, useDirectory } from './useDirectory'

/** The turn states a person has to do something about (harness.turn_status). */
const STUCK_STATES = new Set(['waiting_runner', 'signed_out', 'unrouted', 'blocked', 'paused'])
const TURN_WORDS: Record<string, string> = {
  picking_up: 'Picking it up',
  queued_behind: 'Queued behind another turn',
  waiting_runner: 'Waiting for its runner to come online',
  signed_out: 'Its runner is signed out of Claude',
  unrouted: 'No runner can take it',
  working: 'Working on it',
  blocked: 'Stopped to ask a question',
  paused: 'Its runner went quiet mid-turn',
  done: 'Last turn finished',
  cancelled: 'Last turn cancelled',
  missed: 'Last turn was missed',
  failed: 'Last turn failed',
  lost: 'Last turn was lost',
}

export function TaskDetailView({
  taskKey,
  onClose,
  inPanel = false,
}: {
  taskKey: string
  onClose?: () => void
  inPanel?: boolean
}): JSX.Element {
  const [task, setTask] = useState<TaskDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const reload = useCallback(() => {
    let cancelled = false
    getTask(taskKey)
      .then((t) => {
        if (!cancelled) {
          setTask(t)
          setError(null)
        }
      })
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : 'Could not load the task'))
    return () => {
      cancelled = true
    }
  }, [taskKey])
  useEffect(() => reload(), [reload])
  useResource(TASK_RESOURCE, () => {
    reload()
  })

  const workspace = task?.workspace ?? ''
  const directory = useDirectory(workspace)

  const patch = useCallback(
    async (body: TaskPatch) => {
      setActionError(null)
      try {
        await updateTask(taskKey, body)
        reload()
      } catch (e) {
        setActionError(e instanceof Error ? e.message : 'Could not save')
      }
    },
    [taskKey, reload],
  )

  const act = useCallback(
    async (action: TaskActionName, comment = '') => {
      setActionError(null)
      try {
        await actOn(taskKey, action, comment)
        reload()
      } catch (e) {
        setActionError(e instanceof Error ? e.message : 'Could not do that')
        throw e
      }
    },
    [taskKey, reload],
  )

  if (error) return <p className="p-6 text-sm text-destructive">{error}</p>
  if (!task) return <p className="p-6 text-sm text-muted-foreground">Loading {taskKey}…</p>

  const editable = task.may_edit
  const names = task.status_names
  return (
    <article className="min-w-0" aria-labelledby="task-title" data-testid="task-detail">
      <header className="mb-4">
        <div className="mb-1 flex items-center gap-2 text-[12px] text-muted-foreground">
          <span className="font-mono">{task.key}</span>
          {task.ext_id !== task.key && <span title="The agent's own name for it">· {task.ext_id}</span>}
          {task.project_handle && (
            <>
              <span>·</span>
              <Link to={scopedPath(task.project_address?.split('/')[0] ?? workspace, `/projects/${task.project_handle}`)} className="hover:text-primary">
                {task.project_name}
              </Link>
            </>
          )}
          {task.parent && (
            <>
              <span>·</span>
              <span>
                subtask of{' '}
                <Link to={scopedPath(workspace, `/tasks/${task.parent.key}`)} className="font-mono hover:text-primary">
                  {task.parent.key}
                </Link>
              </span>
            </>
          )}
          <span className="ml-auto flex items-center gap-1">
            {inPanel && (
              <Link to={scopedPath(workspace, `/tasks/${task.key}`)} className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-muted hover:text-foreground">
                <ExternalLink aria-hidden="true" className="size-3.5" /> Open
              </Link>
            )}
            {onClose && (
              <button type="button" onClick={onClose} aria-label="Close" className="rounded p-1 hover:bg-muted hover:text-foreground">
                <X aria-hidden="true" className="size-4" />
              </button>
            )}
          </span>
        </div>
        <EditableTitle value={task.title} disabled={!editable} onSave={(title) => patch({ title })} />
      </header>

      <dl className="mb-5 grid grid-cols-[7rem_1fr] gap-x-3 gap-y-1.5 text-[13px]" data-testid="task-properties">
        <Prop label="Status">
          <StatusPicker status={task.status} names={names} disabled={!editable} onChange={(status) => patch({ status })}>
            <span className="inline-flex items-center gap-1.5">
              <StatusIcon status={task.status} /> {statusName(task.status, names)}
            </span>
          </StatusPicker>
        </Prop>
        <Prop label="Priority">
          <PriorityPicker priority={task.priority ?? 0} disabled={!editable} onChange={(priority) => patch({ priority })}>
            <span>{priorityLabel(task.priority ?? 0)}</span>
          </PriorityPicker>
        </Prop>
        <Prop label="Assignee">
          <AssigneePicker task={task} directory={directory} disabled={!editable} onChange={(assignee) => patch({ assignee })} />
        </Prop>
        <Prop label="Project">
          <ProjectPicker
            project={task.project_address}
            directory={directory}
            disabled={!editable}
            onChange={(project) => patch({ project, ...(task.parent_key ? { parent: '' } : {}) })}
          >
            <span className="text-foreground-secondary">{task.project_name || 'No project'}</span>
          </ProjectPicker>
        </Prop>
        {task.project_address && (
          <Prop label="Milestone">
            <MilestoneSelect task={task} disabled={!editable} onChange={(milestone) => patch({ milestone })} />
          </Prop>
        )}
        <Prop label="Due">
          <DueInput value={task.due} disabled={!editable} onChange={(due) => patch({ due })} />
        </Prop>
        <Prop label="Labels">
          <LabelPicker
            labels={task.labels ?? []}
            directory={directory}
            disabled={!editable}
            onChange={(labels) => patch({ labels })}
            onCreate={async (name) => {
              await createLabel(workspace, name).catch(() => undefined)
              invalidateDirectory(workspace)
              await patch({ labels: [...(task.labels ?? []).map((l) => l.name), name] })
            }}
          >
            {task.labels?.length ? <LabelChips labels={task.labels} max={6} /> : <span className="text-muted-foreground">None</span>}
          </LabelPicker>
        </Prop>
        {task.created_by_agent_slug && (
          <Prop label="Filed by">
            <span className="inline-flex items-center gap-1 text-foreground-secondary">
              <Bot aria-hidden="true" className="size-3.5 text-primary" /> {displayAgent(task.created_by_agent_slug)}
            </span>
          </Prop>
        )}
      </dl>

      {actionError && (
        <p role="alert" className="mb-3 rounded border border-destructive/30 bg-destructive/10 px-3 py-2 text-[12px] text-destructive">
          {actionError}
        </p>
      )}

      {task.turn && <TurnStrip task={task} />}
      <Decision task={task} onAct={act} />

      {(task.human_why?.length ?? 0) > 0 && (
        <Block title="In their words">
          <ul className="space-y-2">
            {task.human_why!.map((w, i) => (
              <li key={i} className="border-l-2 border-primary/40 pl-3 text-[13px]">
                <p className="text-foreground">“{w.quote}”</p>
                <p className="mt-0.5 text-[11.5px] text-muted-foreground">
                  — {w.who.name || w.who.email || 'someone'}
                  {w.source_url && (
                    <>
                      {' · '}
                      <a href={w.source_url} target="_blank" rel="noreferrer" className="hover:text-primary">
                        where they said it
                      </a>
                    </>
                  )}
                </p>
              </li>
            ))}
          </ul>
        </Block>
      )}

      {(task.next_action || task.plan || task.rationale || task.notes) && (
        <Block title="The work">
          {task.next_action && (
            <p className="mb-2 text-[13px] text-foreground">
              <span className="text-muted-foreground">Next: </span>
              {task.next_action}
            </p>
          )}
          {task.plan && <Prose label="Plan" text={task.plan} />}
          {task.rationale && <Prose label={task.agent_slug ? `${displayAgent(task.agent_slug)}'s why` : 'Why'} text={task.rationale} />}
          {task.notes && <Prose label="Notes" text={task.notes} />}
        </Block>
      )}

      <Block title={`Subtasks${task.subtasks?.length ? ` · ${task.subtasks.filter(isOpen).length} open` : ''}`}>
        {(task.subtasks?.length ?? 0) > 0 && (
          <TaskTable
            tasks={task.subtasks ?? []}
            directory={directory}
            columns={['assignee', 'due']}
            onPatch={async (key, body) => {
              await updateTask(key, body).catch((e: unknown) => setActionError(e instanceof Error ? e.message : 'Could not save'))
              reload()
            }}
          />
        )}
        {editable && (
          <AddSubtask
            onAdd={async (title) => {
              await createTask(
                { title, parent: task.key, project: task.project_address ?? '', status: 'todo', workspace },
                workspace,
              )
              reload()
            }}
          />
        )}
      </Block>

      <Relations task={task} editable={editable} onChange={setTask} onError={setActionError} />

      {(task.links?.length ?? 0) > 0 && (
        <Block title="Links">
          <ul className="space-y-1 text-[13px]">
            {task.links!.map((l, i) => (
              <li key={i}>
                <a href={l.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">
                  <Link2 aria-hidden="true" className="size-3.5" /> {l.label || l.url}
                </a>
              </li>
            ))}
          </ul>
        </Block>
      )}

      <Activity task={task} onAct={act} />
    </article>
  )
}

function Prop({ label, children }: { label: string; children: ReactNode }): JSX.Element {
  return (
    <>
      <dt className="py-0.5 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 py-0.5">{children}</dd>
    </>
  )
}

function Block({ title, children }: { title: string; children: ReactNode }): JSX.Element {
  return (
    <section className="mb-5 border-t border-border pt-4">
      <h2 className="mb-2 text-[13px] font-semibold text-foreground">{title}</h2>
      {children}
    </section>
  )
}

function Prose({ label, text }: { label: string; text: string }): JSX.Element {
  return (
    <div className="mb-2">
      <p className="text-[11.5px] font-medium text-muted-foreground">{label}</p>
      <Markdown className="text-[13px]">{text}</Markdown>
    </div>
  )
}

function EditableTitle({ value, disabled, onSave }: { value: string; disabled: boolean; onSave: (v: string) => void }): JSX.Element {
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])
  if (disabled) {
    return (
      <h1 id="task-title" className="text-lg font-semibold text-foreground">
        {value}
      </h1>
    )
  }
  return (
    <h1 id="task-title" className="text-lg font-semibold text-foreground">
      <input
        value={draft}
        aria-label="Title"
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => draft.trim() && draft.trim() !== value && onSave(draft.trim())}
        onKeyDown={(e) => {
          if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
          if (e.key === 'Escape') setDraft(value)
        }}
        className="w-full rounded border border-transparent bg-transparent px-1 -mx-1 hover:border-input focus:border-input focus:outline-none"
      />
    </h1>
  )
}

function MilestoneSelect({
  task,
  disabled,
  onChange,
}: {
  task: TaskDetail
  disabled: boolean
  onChange: (name: string) => void
}): JSX.Element {
  const [ms, setMs] = useState<Milestone[] | null>(null)
  const [ws, handle] = (task.project_address ?? '/').split('/')
  useEffect(() => {
    if (!ws || !handle) return
    let cancelled = false
    listMilestones(ws, handle)
      .then((rows) => !cancelled && setMs(rows))
      .catch(() => !cancelled && setMs([]))
    return () => {
      cancelled = true
    }
  }, [ws, handle])
  if (!ms?.length) return <span className="text-muted-foreground">{task.milestone || 'None'}</span>
  return (
    <select
      value={task.milestone ?? ''}
      disabled={disabled}
      aria-label="Milestone"
      onChange={(e) => onChange(e.target.value)}
      className="rounded border border-input bg-input px-1.5 py-0.5 text-[12px] text-foreground"
    >
      <option value="">None</option>
      {ms.map((m) => (
        <option key={m.id} value={m.name}>
          {m.name}
        </option>
      ))}
    </select>
  )
}

function TurnStrip({ task }: { task: TaskDetail }): JSX.Element {
  const turn = task.turn!
  const stuck = STUCK_STATES.has(turn.state)
  const live = !['done', 'cancelled', 'missed', 'failed', 'lost'].includes(turn.state)
  return (
    <div
      className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-[12.5px]"
      data-testid="turn-strip"
    >
      <Bot aria-hidden="true" className="size-4 text-primary" />
      <span className="font-medium text-foreground">{task.agent_slug ? displayAgent(task.agent_slug) : 'Agent'}</span>
      <StatusBadge tone={stuck ? 'warning' : live ? 'primary' : turn.state === 'done' ? 'success' : 'neutral'} pulse={live && !stuck}>
        {TURN_WORDS[turn.state] ?? turn.state}
      </StatusBadge>
      <span className="text-muted-foreground">{relativeAge(turn.created_at, new Date(), { seconds: false })}</span>
      {turn.url && (
        <Link to={turn.url} className="ml-auto text-primary hover:underline">
          Turn log
        </Link>
      )}
    </div>
  )
}

/** What it asks of you, and the buttons that answer it. Approve means START NOW. */
function Decision({ task, onAct }: { task: TaskDetail; onAct: (a: TaskActionName, c?: string) => Promise<void> }): JSX.Element | null {
  const [busy, setBusy] = useState(false)
  const triage = task.status === 'suggested'
  const asking = Boolean(task.ask_kind && task.ask_open)
  if (!triage && !asking) return null
  const run = async (a: TaskActionName, c = '') => {
    setBusy(true)
    try {
      await onAct(a, c)
    } catch {
      /* the error is shown above */
    } finally {
      setBusy(false)
    }
  }
  const starts = task.agent_slug ? `${displayAgent(task.agent_slug)} starts on it now` : 'it moves to In progress'
  return (
    <section className="mb-5 rounded-lg border border-warning/30 bg-warning/5 p-3" data-testid="task-decision">
      <p className="mb-1 text-[12px] font-semibold text-warning">
        {task.ask_kind === 'question' ? 'Asks you a question' : asking ? 'Asks for your decision' : 'Suggested — decide whether to take it on'}
      </p>
      {task.ask_body && <Markdown className="mb-2 text-[13px]">{task.ask_body}</Markdown>}
      {task.ask_kind !== 'question' && (
        <div className="flex flex-wrap gap-2">
          <Button size="sm" disabled={busy} onClick={() => run('approve')} title={`Approve: ${starts}`}>
            Start now
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => run('accept')} title="Accept it into Todo; nothing starts">
            Accept
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => run('backlog')} title="Accepted, not now">
            Backlog
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => run('snooze')} title="Out of Triage for a week, or until something happens on it">
            Snooze
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => run('decline')}>
            Decline
          </Button>
        </div>
      )}
      {task.ask_kind !== 'question' && <p className="mt-1.5 text-[11.5px] text-muted-foreground">Start now: {starts}.</p>}
      {task.ask_kind === 'question' && <p className="text-[11.5px] text-muted-foreground">Answer it in the reply box below.</p>}
    </section>
  )
}

function AddSubtask({ onAdd }: { onAdd: (title: string) => Promise<void> }): JSX.Element {
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  return (
    <form
      className="mt-2 flex gap-2"
      onSubmit={async (e) => {
        e.preventDefault()
        if (!title.trim()) return
        setBusy(true)
        try {
          await onAdd(title.trim())
          setTitle('')
        } finally {
          setBusy(false)
        }
      }}
    >
      <input
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Add a subtask…"
        aria-label="New subtask"
        className="min-w-0 flex-1 rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground placeholder:text-placeholder"
      />
      <Button size="sm" variant="outline" type="submit" disabled={busy || !title.trim()}>
        Add
      </Button>
    </form>
  )
}

const RELATION_WORDS: Record<string, string> = {
  blocks: 'Blocks',
  blocked_by: 'Blocked by',
  related: 'Related to',
  duplicate_of: 'Duplicate of',
  duplicated_by: 'Duplicated by',
}

function Relations({
  task,
  editable,
  onChange,
  onError,
}: {
  task: TaskDetail
  editable: boolean
  onChange: (t: TaskDetail) => void
  onError: (e: string | null) => void
}): JSX.Element | null {
  const [kind, setKind] = useState<'blocks' | 'blocked_by' | 'related' | 'duplicate'>('blocked_by')
  const [other, setOther] = useState('')
  const rels = task.relations ?? []
  if (!rels.length && !editable) return null
  const storedKind = (k: string) => (k === 'duplicate_of' || k === 'duplicated_by' ? 'duplicate' : k)
  return (
    <Block title={task.blocked ? 'Relations · blocked' : 'Relations'}>
      {rels.length > 0 && (
        <ul className="mb-2 space-y-1 text-[13px]">
          {rels.map((r) => (
            <li key={`${r.kind}-${r.task.key}`} className="flex items-center gap-2">
              <span className="w-24 shrink-0 text-muted-foreground">{RELATION_WORDS[r.kind] ?? r.kind}</span>
              <StatusIcon status={r.task.status as TaskDetail['status']} />
              <Link to={r.task.url} className="font-mono text-[12px] text-muted-foreground hover:text-primary">
                {r.task.key}
              </Link>
              <span className="truncate text-foreground">{r.task.title}</span>
              {editable && (
                <button
                  type="button"
                  aria-label={`Remove ${RELATION_WORDS[r.kind]} ${r.task.key}`}
                  className="ml-auto rounded p-0.5 text-muted-foreground hover:bg-muted"
                  onClick={async () => {
                    try {
                      onChange(await removeRelation(task.key, storedKind(r.kind), r.task.key))
                    } catch (e) {
                      onError(e instanceof Error ? e.message : 'Could not remove it')
                    }
                  }}
                >
                  <X aria-hidden="true" className="size-3.5" />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {editable && (
        <form
          className="flex flex-wrap gap-2"
          onSubmit={async (e) => {
            e.preventDefault()
            if (!other.trim()) return
            try {
              onChange(await addRelation(task.key, kind, other.trim().toUpperCase()))
              setOther('')
              onError(null)
            } catch (err) {
              onError(err instanceof Error ? err.message : 'Could not relate them')
            }
          }}
        >
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as typeof kind)}
            aria-label="Relation"
            className="rounded border border-input bg-input px-1.5 py-1 text-[12px] text-foreground"
          >
            <option value="blocked_by">Blocked by</option>
            <option value="blocks">Blocks</option>
            <option value="related">Related to</option>
            <option value="duplicate">Duplicate of</option>
          </select>
          <input
            value={other}
            onChange={(e) => setOther(e.target.value)}
            placeholder="Task key, e.g. DIM-12"
            aria-label="Other task"
            className="w-40 rounded border border-input bg-input px-2 py-1 text-[12px] text-foreground placeholder:text-placeholder"
          />
          <Button size="sm" variant="outline" type="submit" disabled={!other.trim()}>
            Relate
          </Button>
        </form>
      )}
    </Block>
  )
}

const FIELD_WORDS: Record<string, string> = {
  title: 'title',
  status: 'status',
  assignee: 'assignee',
  project: 'project',
  parent: 'parent',
  priority: 'priority',
  due: 'due date',
  milestone: 'milestone',
  labels: 'labels',
  next_action: 'next action',
  kind: 'kind',
  waiting_on: 'who it waits on',
  key: 'key',
}

const ACTION_WORDS: Record<string, string> = {
  approve: 'started it',
  decline: 'declined it',
  reply: 'replied',
  nudge: 'nudged the agent',
  done: 'marked it done',
  accept: 'accepted it',
  backlog: 'moved it to the backlog',
  snooze: 'snoozed it',
  duplicate: 'marked it a duplicate',
}

function shown(field: string, value: string, names?: Record<string, string> | null): string {
  if (!value) return 'none'
  if (field === 'status') return statusName(value as TaskDetail['status'], names)
  if (field === 'priority') return priorityLabel(Number(value))
  if (field === 'assignee') return value.startsWith('agent:') ? displayAgent(value.slice(6)) : displayPerson(value)
  return value
}

function eventLine(e: TaskEvent, names?: Record<string, string> | null): string {
  const who = e.by_agent ? displayAgent(e.by_agent) : e.by_name || (e.by_email ? displayPerson(e.by_email) : 'Someone')
  if (e.kind === 'created') return `${who} filed it`
  if (e.kind === 'action') return `${who} ${ACTION_WORDS[e.field] ?? e.field}`
  if (e.kind === 'relation') {
    const rel = RELATION_WORDS[e.field] ?? e.field
    return e.new ? `${who} linked ${e.new} (${rel.toLowerCase()})` : `${who} unlinked ${e.old} (${rel.toLowerCase()})`
  }
  const f = FIELD_WORDS[e.field] ?? e.field
  if (e.field === 'title' || e.field === 'next_action') return `${who} changed the ${f} to “${e.new}”`
  return `${who} changed the ${f} from ${shown(e.field, e.old, names)} to ${shown(e.field, e.new, names)}`
}

function Activity({ task, onAct }: { task: TaskDetail; onAct: (a: TaskActionName, c?: string) => Promise<void> }): JSX.Element {
  const events = useMemo(() => [...(task.events ?? [])].reverse(), [task.events])
  const [reply, setReply] = useState('')
  const [busy, setBusy] = useState(false)
  const question = task.ask_kind === 'question' && task.ask_open
  const doneish = STATUS_CATEGORY[task.status] === 'completed' || STATUS_CATEGORY[task.status] === 'canceled'
  return (
    <Block title="Activity">
      <form
        className="mb-3"
        onSubmit={async (e) => {
          e.preventDefault()
          if (!reply.trim()) return
          setBusy(true)
          try {
            await onAct('reply', reply.trim())
            setReply('')
          } catch {
            /* shown above */
          } finally {
            setBusy(false)
          }
        }}
      >
        <textarea
          value={reply}
          onChange={(e) => setReply(e.target.value)}
          rows={2}
          aria-label={question ? 'Answer' : 'Reply'}
          placeholder={
            question
              ? 'Answer the question — the agent picks it up now…'
              : task.agent_slug
                ? `Reply — ${displayAgent(task.agent_slug)} reads it…`
                : 'Leave a comment…'
          }
          className="w-full rounded border border-input bg-input px-2 py-1.5 text-[13px] text-foreground placeholder:text-placeholder"
        />
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <Button size="sm" type="submit" disabled={busy || !reply.trim()}>
            {question ? 'Answer' : 'Reply'}
          </Button>
          {task.may_edit && task.status === 'in_progress' && task.agent_slug && (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => onAct('nudge').catch(() => undefined)}>
              Nudge {displayAgent(task.agent_slug)}
            </Button>
          )}
          {task.may_edit && !doneish && (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => onAct('done').catch(() => undefined)}>
              Mark done
            </Button>
          )}
        </div>
      </form>
      <ol className="space-y-2" data-testid="task-activity">
        {events.map((e) => (
          <li key={e.id} className="text-[12.5px]">
            <span className="text-foreground-secondary">{eventLine(e, task.status_names)}</span>
            <span className="ml-2 text-[11px] text-muted-foreground" title={e.created_at}>
              {relativeAge(e.created_at, new Date(), { seconds: false })}
            </span>
            {e.comment && <Markdown className="mt-1 rounded border border-border bg-card px-2.5 py-1.5 text-[13px]">{e.comment}</Markdown>}
          </li>
        ))}
      </ol>
    </Block>
  )
}
