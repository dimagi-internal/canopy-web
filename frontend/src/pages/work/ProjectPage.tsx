// /w/:workspace/projects/:handle — a project's ONE page, for teammates and
// editors alike. In order: its properties (status, health, lead, lead agent
// and the other agents, dates, priority, outcome, where its files are); the
// latest update, or a nudge when one is due, and the form to post one; its
// milestones; scope against done; every task in it; then what it proposes, its
// ideas and people, its check-ins and the turns that touched it. A teammate
// sees all of it; the controls they may not use (`may_edit`) are not drawn.
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type JSX, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Pencil, Trash2, X } from 'lucide-react'
import { Button, StatusBadge } from 'canopy-ui/ui'
import { WorkbenchSkeleton } from 'canopy-ui'

import {
  createMilestone,
  createTask,
  deleteMilestone,
  getProjectByAddress,
  listTasks,
  postUpdate,
  publishUpdate,
  updateMilestone,
  updateProject,
  type Health,
  type Milestone,
  type ProjectDetail,
  type ProjectPatch,
  type ProjectUpdate,
} from '@/api/work'
import { Markdown } from '@/components/Markdown'
import { Section } from '@/components/objectPage/settingsLayout'
import { ProposalCard } from '@/components/projects/ProposalCard'
import { useEngagementView } from '@/components/projects/useEngagementView'
import { PriorityPicker } from '@/components/work/bits'
import { HEALTHS, PROJECT_STATUSES, displayAgent, displayPerson, healthMeta, priorityLabel } from '@/components/work/meta'
import { AgentPicker, AgentsPicker, HealthBadge, PersonPicker, ProgressBar, ProjectStatusBadge } from '@/components/work/projectBits'
import { ScopeChart, scopePoints } from '@/components/work/ScopeChart'
import { TaskPeek } from '@/components/work/TaskPeek'
import { TaskTable } from '@/components/work/TaskTable'
import { useDirectory } from '@/components/work/useDirectory'
import { usePeek } from '@/components/work/usePeek'
import { useTaskList } from '@/components/work/useTaskList'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'
import { CheckinsPanel, CheckinTimeline } from '@/pages/agents/ProjectCheckins'
import { ProjectOverview } from '@/pages/agents/ProjectOverview'
import { ProjectKbLink, ProjectMcpInstall } from '@/pages/projects/WorkspaceProjects'
import { TASK_RESOURCE, useResource } from '@/widget/useResource'

/** Open work only, unless "Show done and canceled" is on. */
const OPEN_CATEGORIES = 'triage,backlog,unstarted,started'
/** How long a burst of task changes waits before the page's counts re-read. */
const REFRESH_DEBOUNCE_MS = 600

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback
}

export function ProjectPage(): JSX.Element {
  const { workspace = '', handle = '' } = useParams()
  const address = `${workspace}/${handle}`
  const [state, setState] = useState<{ key: string; detail: ProjectDetail | null; error: string | null } | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [timelineVersion, setTimelineVersion] = useState(0)

  const reload = useCallback(() => {
    let cancelled = false
    const key = `${workspace}/${handle}`
    getProjectByAddress(workspace, handle)
      .then((detail) => !cancelled && setState({ key, detail, error: null }))
      .catch((err: unknown) => {
        if (cancelled) return
        const status = (err as { status?: number } | null)?.status
        setState({ key, detail: null, error: status === 404 ? `No project ${key}.` : errorText(err, 'Could not load the project') })
      })
    return () => {
      cancelled = true
    }
  }, [workspace, handle])
  useEffect(() => reload(), [reload])

  // A task on this project moved: its progress, milestones and turns move too.
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useResource(TASK_RESOURCE, () => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(reload, REFRESH_DEBOUNCE_MS)
  })
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])

  const fresh = state?.key === address ? state : null
  const detail = fresh?.detail ?? null
  useEngagementView(detail ? { project: detail.id, workspace: detail.workspace } : null)

  const save = useCallback(
    async (body: ProjectPatch) => {
      setSaveError(null)
      try {
        await updateProject(workspace, handle, body)
      } catch (err) {
        setSaveError(errorText(err, 'Could not save'))
      }
      reload()
    },
    [workspace, handle, reload],
  )

  const back = (
    <Link to={scopedPath(workspace, '/projects')} className="text-[12px] text-muted-foreground hover:text-foreground">
      ← Projects
    </Link>
  )

  if (!fresh) {
    return (
      <div className="max-w-5xl">
        {back}
        <WorkbenchSkeleton />
      </div>
    )
  }
  if (!detail) {
    return (
      <div className="max-w-5xl">
        {back}
        <p className="mt-4 text-[13px] text-destructive">{fresh.error}</p>
      </div>
    )
  }

  const lead = detail.agent_slug || detail.steward_slug || ''
  // The agent-scoped routes (ideas, check-ins) know the project by its alias on its lead agent.
  const agentRef = detail.ext_id || detail.handle
  const turns = detail.recent_turns ?? []

  return (
    // pb-28: room under the last section for the site-wide floating "Canopy AI" button.
    <div className="max-w-5xl pb-28" data-testid="project-page">
      {back}
      <PropertyHeader detail={detail} workspace={workspace} onSave={save} />
      {saveError && (
        <p role="alert" className="mb-4 rounded border border-destructive/30 bg-destructive/10 px-3 py-2 text-[12px] text-destructive">
          {saveError}
        </p>
      )}

      <UpdatesSection detail={detail} workspace={workspace} handle={handle} onChanged={reload} />
      <ProjectWork detail={detail} workspace={workspace} handle={handle} address={address} onChanged={reload} />

      {detail.proposal && (
        <Section id="project-proposal" title="Proposal" description="This project was proposed. Start it or decline it here.">
          <ProposalCard proposal={detail.proposal} onChanged={reload} />
        </Section>
      )}

      {(detail.proposed_tasks ?? []).length > 0 && (
        <Section
          id="project-proposed"
          title="Proposed tasks"
          description="Tasks this project's check-ins proposed. Start one, decline it with a reason, back it or ask — the agent reads it before the next check-in."
        >
          <div className="grid gap-4 lg:grid-cols-2">
            {(detail.proposed_tasks ?? []).map((t) => (
              <ProposalCard
                key={`${t.agent}-${t.ref}`}
                task={t}
                onChanged={() => {
                  reload()
                  setTimelineVersion((v) => v + 1)
                }}
              />
            ))}
          </div>
        </Section>
      )}

      {lead && agentRef && (
        <>
          <ProjectOverview
            agent={lead}
            projectRef={agentRef}
            pageHref={(personId) =>
              scopedPath(workspace, `/agents/${lead}/projects/${encodeURIComponent(agentRef)}/people/${personId}`)
            }
            showRefs={detail.may_edit}
            bare
          />
          <Section
            id="project-checkins"
            title="Check-ins"
            description="This project's recurring meetings: who leads each one, which agents take part, the cadence, and the Slack channel it reads — only when its lead presses Confirm there."
          >
            <CheckinsPanel
              project={{ agent: lead, ref: agentRef, workspace: detail.workspace || workspace }}
              projectAgent={lead}
              canAdd={detail.may_edit}
              onRan={() => setTimelineVersion((v) => v + 1)}
            />
          </Section>
          <Section
            id="project-timeline"
            title="Timeline"
            description="Every check-in run, newest first: who took part, what was read, who confirmed it, and what it proposed."
          >
            <CheckinTimeline project={{ agent: lead, ref: agentRef, workspace: detail.workspace || workspace }} version={timelineVersion} />
          </Section>
        </>
      )}

      <Section id="project-activity" title="Recent turns" description="Agent turns that touched one of this project's tasks, newest first.">
        {turns.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">No turns have touched this project yet.</p>
        ) : (
          <ul className="divide-y divide-border rounded-lg border border-border bg-card">
            {turns.map((turn) => (
              <li key={turn.id}>
                <Link
                  to={lead ? scopedPath(workspace, `/agents/${lead}/turns#${turn.id}`) : scopedPath(workspace, '/activity')}
                  data-testid={`turn-${turn.id}`}
                  className="flex items-baseline gap-2 px-3 py-1.5 text-[13px] hover:bg-muted/50"
                >
                  <span className="w-20 shrink-0 text-[11.5px] text-muted-foreground">{turn.status}</span>
                  <span className="min-w-0 flex-1 truncate text-foreground">{turn.prompt_preview || '(no prompt)'}</span>
                  <span className="shrink-0 text-[11.5px] text-muted-foreground">
                    {relativeAge(turn.created_at, new Date(), { seconds: false })}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <TaskPeek />
    </div>
  )
}

// ---- the property header ------------------------------------------------------

function PropertyHeader({
  detail,
  workspace,
  onSave,
}: {
  detail: ProjectDetail
  workspace: string
  onSave: (body: ProjectPatch) => Promise<void>
}): JSX.Element {
  const editable = detail.may_edit
  const directory = useDirectory(workspace)
  const lead = detail.agent_slug || detail.steward_slug || ''
  const members = detail.member_agents ?? []
  const links = detail.links ?? []
  return (
    <header className="mb-8 mt-3 border-b border-border pb-5" data-testid="project-header">
      <EditableText
        value={detail.name}
        disabled={!editable}
        label="Project name"
        onSave={(name) => void onSave({ name })}
        className="text-xl font-semibold text-foreground"
      />
      <dl className="mt-3 grid grid-cols-[7rem_1fr] gap-x-3 gap-y-1.5 text-[13px] sm:grid-cols-[7rem_1fr_7rem_1fr]" data-testid="project-properties">
        <Prop label="Status">
          <StatusControl detail={detail} onChange={(status) => void onSave({ status })} />
        </Prop>
        <Prop label="Health">
          <HealthBadge project={detail} />
          {!detail.health && !detail.update_missing && <span className="text-foreground-subtle">No update yet</span>}
        </Prop>
        <Prop label="Lead">
          <PersonPicker email={detail.owner_email} directory={directory} disabled={!editable} onChange={(owner_email) => void onSave({ owner_email })} />
        </Prop>
        <Prop label="Lead agent">
          <AgentPicker slug={lead} directory={directory} disabled={!editable} onChange={(steward) => void onSave({ steward })} />
        </Prop>
        <Prop label="Agents">
          <AgentsPicker
            slugs={members}
            lead={lead}
            directory={directory}
            disabled={!editable}
            onChange={(member_agents) => void onSave({ member_agents })}
          />
        </Prop>
        <Prop label="Priority">
          <PriorityPicker priority={detail.priority ?? 0} disabled={!editable} onChange={(priority) => void onSave({ priority })}>
            <span>{priorityLabel(detail.priority ?? 0)}</span>
          </PriorityPicker>
        </Prop>
        <Prop label="Start">
          <DateField label="Start date" value={detail.start_date} disabled={!editable} onChange={(start_date) => void onSave({ start_date })} />
        </Prop>
        <Prop label="Target">
          <DateField label="Target date" value={detail.target_date} disabled={!editable} onChange={(target_date) => void onSave({ target_date })} />
        </Prop>
      </dl>
      <div className="mt-3">
        {editable ? (
          <EditableText
            value={detail.outcome}
            multiline
            disabled={false}
            label="Outcome"
            placeholder="What done looks like"
            onSave={(outcome) => void onSave({ outcome })}
            className="text-[14px] leading-relaxed text-foreground-secondary"
          />
        ) : (
          detail.outcome && (
            <p className="text-[14px] leading-relaxed text-foreground-secondary" data-testid="project-outcome">
              {detail.outcome}
            </p>
          )
        )}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-muted-foreground" data-testid="project-where">
        <span className="font-mono">{detail.address}</span>
        {detail.ext_id && lead && (
          <span>
            {displayAgent(lead)}’s {detail.ext_id}
          </span>
        )}
        {detail.drive_folder_url && (
          <a href={detail.drive_folder_url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
            Drive folder
          </a>
        )}
        {detail.repo_slug && <span>Repo: {detail.repo_slug}</span>}
        {links.map((l) => (
          <a key={`${l.label}|${l.url}`} href={l.url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
            {l.label || l.url}
          </a>
        ))}
        {detail.spawned_from && (
          <span data-testid="spawned-from">
            From{' '}
            <Link to={detail.spawned_from.url} className="text-primary hover:underline">
              {detail.spawned_from.ref} {detail.spawned_from.title}
            </Link>
            {detail.spawned_from.project_name ? ` in ${detail.spawned_from.project_name}` : ''}
          </span>
        )}
      </div>
      <ProjectKbLink project={detail} />
      <ProjectMcpInstall project={detail} />
    </header>
  )
}

/** A proposed project is started only from its decision card (the server says 409 otherwise). */
function StatusControl({ detail, onChange }: { detail: ProjectDetail; onChange: (s: string) => void }): JSX.Element {
  if (!detail.may_edit || detail.status === 'proposed') {
    return (
      <span className="inline-flex items-center gap-2">
        <ProjectStatusBadge status={detail.status} />
        {detail.may_edit && detail.proposal && (
          <a href="#project-proposal" className="text-[12px] text-primary hover:underline">
            Decide below
          </a>
        )}
      </span>
    )
  }
  return (
    <select
      aria-label="Project status"
      value={detail.status}
      onChange={(e) => onChange(e.target.value)}
      className="rounded border border-input bg-input px-1.5 py-0.5 text-[12px] text-foreground"
    >
      {PROJECT_STATUSES.filter((s) => s.value !== 'proposed').map((s) => (
        <option key={s.value} value={s.value}>
          {s.label}
        </option>
      ))}
    </select>
  )
}

function Prop({ label, children }: { label: string; children: ReactNode }): JSX.Element {
  return (
    <>
      <dt className="py-0.5 text-muted-foreground">{label}</dt>
      <dd className="flex min-w-0 items-center gap-2 py-0.5">{children}</dd>
    </>
  )
}

function DateField({
  label,
  value,
  onChange,
  disabled,
}: {
  label: string
  value: string | null | undefined
  onChange: (d: string | null) => void
  disabled?: boolean
}): JSX.Element {
  if (disabled) return <span className="text-foreground-secondary">{value || <span className="text-foreground-subtle">—</span>}</span>
  return (
    <input
      type="date"
      aria-label={label}
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value || null)}
      className="w-[8.5rem] rounded border border-transparent bg-transparent px-1 text-[12px] text-foreground-secondary hover:border-input focus:border-input"
    />
  )
}

function EditableText({
  value,
  onSave,
  disabled,
  label,
  className,
  multiline = false,
  placeholder,
}: {
  value: string
  onSave: (v: string) => void
  disabled: boolean
  label: string
  className: string
  multiline?: boolean
  placeholder?: string
}): JSX.Element {
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])
  if (disabled) return multiline ? <p className={className}>{value}</p> : <h1 className={className}>{value}</h1>
  const commit = () => {
    const v = draft.trim()
    if (v !== value.trim() && (v || multiline)) onSave(v)
    else setDraft(value)
  }
  const field = 'w-full rounded border border-transparent bg-transparent px-1 -mx-1 hover:border-input focus:border-input focus:outline-none'
  if (multiline) {
    return (
      <textarea
        value={draft}
        aria-label={label}
        placeholder={placeholder}
        rows={2}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        className={`${field} resize-y placeholder:text-placeholder ${className}`}
      />
    )
  }
  return (
    <h1 className={className}>
      <input
        value={draft}
        aria-label={label}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
          if (e.key === 'Escape') setDraft(value)
        }}
        className={field}
      />
    </h1>
  )
}

// ---- updates --------------------------------------------------------------------

function authorOf(u: ProjectUpdate): string {
  if (u.author_agent) return displayAgent(u.author_agent)
  if (u.author_email) return displayPerson(u.author_email)
  return 'someone'
}

/** "3 tasks done · 2 added · target Nov 1 → Nov 15 · Beta 4/6" from an update's `diff`. */
export function diffSummary(diff: Record<string, unknown> | undefined | null): string[] {
  if (!diff) return []
  const out: string[] = []
  const done = Number(diff.tasks_done ?? 0)
  const added = Number(diff.tasks_added ?? 0)
  if (done) out.push(`${done} task${done === 1 ? '' : 's'} done`)
  if (added) out.push(`${added} added`)
  const target = diff.target_date
  if (Array.isArray(target) && target.length === 2) out.push(`target ${target[0] ?? 'none'} → ${target[1] ?? 'none'}`)
  const ms = Array.isArray(diff.milestones) ? (diff.milestones as { name?: string; done?: number; total?: number }[]) : []
  for (const m of ms) if (m?.name) out.push(`${m.name} ${m.done ?? 0}/${m.total ?? 0}`)
  return out
}

function HealthChip({ health }: { health: string }): JSX.Element | null {
  const h = healthMeta(health)
  return h ? (
    <StatusBadge tone={h.tone} dot>
      {h.label}
    </StatusBadge>
  ) : null
}

function UpdatesSection({
  detail,
  workspace,
  handle,
  onChanged,
}: {
  detail: ProjectDetail
  workspace: string
  handle: string
  onChanged: () => void
}): JSX.Element {
  const latest = detail.latest_update ?? null
  const drafts = detail.draft_updates ?? []
  const diff = diffSummary(latest?.diff)
  return (
    <Section id="project-update" title="Latest update" description="How it is going, in a line and a health: on track, at risk or off track.">
      {detail.update_missing && (
        <p className="mb-3 rounded border border-warning/30 bg-warning/10 px-3 py-2 text-[12px] text-warning" data-testid="update-missing">
          Update missing — this project has had no update for longer than the workspace expects.
          {detail.may_edit ? ' Post one below.' : ''}
        </p>
      )}
      {latest ? (
        <article className="rounded-lg border border-border bg-card px-4 py-3" data-testid="latest-update">
          <div className="mb-2 flex flex-wrap items-center gap-2 text-[12px] text-muted-foreground">
            <HealthChip health={latest.health} />
            <span>
              {authorOf(latest)}
              {latest.author_agent && latest.published_by_email ? `, published by ${displayPerson(latest.published_by_email)}` : ''}
            </span>
            <span title={latest.published_at ?? latest.created_at}>
              {relativeAge(latest.published_at ?? latest.created_at, new Date(), { seconds: false })}
            </span>
          </div>
          {latest.body ? <Markdown className="text-[13px]">{latest.body}</Markdown> : <p className="text-[13px] text-muted-foreground">No words, just the health.</p>}
          {diff.length > 0 && (
            <p className="mt-2 text-[12px] text-muted-foreground" data-testid="update-diff">
              Since the last update: {diff.join(' · ')}
            </p>
          )}
        </article>
      ) : (
        <p className="text-[13px] text-muted-foreground">No update yet.</p>
      )}

      {drafts.length > 0 && (
        <div className="mt-4" data-testid="draft-updates">
          <h3 className="mb-1 text-[12px] font-medium text-muted-foreground">Drafts waiting to be published ({drafts.length})</h3>
          <ul className="divide-y divide-border rounded-lg border border-border bg-card">
            {drafts.map((d) => (
              <DraftRow key={d.id} draft={d} canPublish={detail.may_edit} workspace={workspace} handle={handle} onChanged={onChanged} />
            ))}
          </ul>
        </div>
      )}

      {detail.may_edit && <PostUpdate workspace={workspace} handle={handle} onPosted={onChanged} />}
    </Section>
  )
}

function HealthRadios({ value, onChange, name }: { value: Health; onChange: (h: Health) => void; name: string }): JSX.Element {
  return (
    <span role="radiogroup" aria-label="Health" className="inline-flex flex-wrap gap-3 text-[12px]">
      {HEALTHS.map((h) => (
        <label key={h.value} className="inline-flex items-center gap-1 text-foreground-secondary">
          <input type="radio" name={name} value={h.value} checked={value === h.value} onChange={() => onChange(h.value)} className="accent-primary" />
          {h.label}
        </label>
      ))}
    </span>
  )
}

function PostUpdate({ workspace, handle, onPosted }: { workspace: string; handle: string; onPosted: () => void }): JSX.Element {
  const [health, setHealth] = useState<Health>('on_track')
  const [body, setBody] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    postUpdate(workspace, handle, { health, body: body.trim() })
      .then(() => {
        setBody('')
        onPosted()
      })
      .catch((err: unknown) => setError(errorText(err, 'Could not post the update')))
      .finally(() => setBusy(false))
  }
  return (
    <form onSubmit={submit} className="mt-4 flex flex-col gap-2" data-testid="post-update">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-[12px] font-medium text-foreground">Post an update</span>
        <HealthRadios value={health} onChange={setHealth} name="post-update-health" />
      </div>
      <textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        aria-label="Update"
        rows={3}
        placeholder="What moved, what is next, what is in the way (Markdown)"
        className="rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground placeholder:text-placeholder"
      />
      {error && <p className="text-[12px] text-destructive">{error}</p>}
      <div>
        <Button type="submit" size="sm" disabled={busy}>
          Post update
        </Button>
      </div>
    </form>
  )
}

function DraftRow({
  draft,
  canPublish,
  workspace,
  handle,
  onChanged,
}: {
  draft: ProjectUpdate
  canPublish: boolean
  workspace: string
  handle: string
  onChanged: () => void
}): JSX.Element {
  // The agent's suggested health, which the publisher may change.
  const [health, setHealth] = useState<Health>(HEALTHS.find((h) => h.value === draft.health)?.value ?? 'on_track')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  return (
    <li className="px-3 py-2 text-[13px]" data-testid={`draft-${draft.id}`}>
      <div className="mb-1 flex flex-wrap items-center gap-2 text-[12px] text-muted-foreground">
        <span>Draft by {authorOf(draft)}</span>
        <span>{relativeAge(draft.created_at, new Date(), { seconds: false })}</span>
      </div>
      {draft.body && <Markdown className="text-[13px]">{draft.body}</Markdown>}
      {canPublish && (
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <HealthRadios value={health} onChange={setHealth} name={`draft-${draft.id}-health`} />
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => {
              setBusy(true)
              setError(null)
              publishUpdate(workspace, handle, draft.id, { health })
                .then(onChanged)
                .catch((err: unknown) => setError(errorText(err, 'Could not publish')))
                .finally(() => setBusy(false))
            }}
          >
            Publish
          </Button>
        </div>
      )}
      {error && <p className="mt-1 text-[12px] text-destructive">{error}</p>}
    </li>
  )
}

// ---- milestones, scope, tasks ------------------------------------------------------

/** The milestones, the graph and the task table share one filter (a milestone's tasks). */
function ProjectWork({
  detail,
  workspace,
  handle,
  address,
  onChanged,
}: {
  detail: ProjectDetail
  workspace: string
  handle: string
  address: string
  onChanged: () => void
}): JSX.Element {
  const [milestone, setMilestone] = useState<string | null>(null)
  const [showClosed, setShowClosed] = useState(false)
  const directory = useDirectory(workspace)
  const { open } = usePeek()
  const list = useTaskList(
    () => listTasks({ project: address, ...(showClosed ? {} : { category: OPEN_CATEGORIES }), limit: 500 }, workspace),
    [address, showClosed],
  )
  const [taskError, setTaskError] = useState<string | null>(null)
  const tasks = useMemo(
    () => (list.tasks ?? []).filter((t) => !milestone || t.milestone === milestone),
    [list.tasks, milestone],
  )
  const progress = detail.progress
  const points = scopePoints(progress?.scope_history)

  return (
    <>
      <MilestonesSection
        milestones={detail.milestones ?? []}
        editable={detail.may_edit}
        workspace={workspace}
        handle={handle}
        selected={milestone}
        onSelect={setMilestone}
        onChanged={onChanged}
      />

      <Section id="project-scope" title="Scope and done" description="Tasks in scope against tasks done, week by week. Canceled tasks are out of scope.">
        {progress && (
          <p className="mb-2 flex flex-wrap items-center gap-3 text-[12px] text-muted-foreground" data-testid="project-progress">
            <ProgressBar done={progress.done} total={progress.total} />
            <span>
              {progress.done} of {progress.total} done · {progress.started} in progress
            </span>
          </p>
        )}
        <ScopeChart points={points} />
      </Section>

      <Section id="project-tasks" title="Tasks" description="Every task in this project, its subtasks under it. Open one to see it beside the list.">
        <div className="mb-2 flex flex-wrap items-center gap-3 text-[12px]">
          {milestone && (
            <span className="inline-flex items-center gap-1 rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-primary" data-testid="milestone-filter">
              Milestone: {milestone}
              <button type="button" aria-label="Show every milestone" onClick={() => setMilestone(null)} className="hover:text-foreground">
                <X aria-hidden="true" className="size-3" />
              </button>
            </span>
          )}
          <label className="inline-flex items-center gap-1.5 text-muted-foreground">
            <input type="checkbox" checked={showClosed} onChange={(e) => setShowClosed(e.target.checked)} className="accent-primary" />
            Show done and canceled
          </label>
        </div>
        {(list.error || taskError) && <p className="mb-2 text-[12px] text-destructive">{taskError || list.error}</p>}
        {list.tasks === null ? (
          <WorkbenchSkeleton />
        ) : (
          <TaskTable
            tasks={tasks}
            directory={directory}
            nest
            groupBy="status"
            columns={['labels', 'assignee', 'due', 'updated']}
            onOpen={(t) => open(t.key)}
            onPatch={(key, body, optimistic) => {
              setTaskError(null)
              list.patch(key, body, optimistic).catch((err: unknown) => setTaskError(errorText(err, 'Could not save')))
            }}
            empty={milestone ? `No ${showClosed ? '' : 'open '}tasks in ${milestone}.` : showClosed ? 'No tasks in this project yet.' : 'No open tasks.'}
          />
        )}
        {detail.may_edit && (
          <AddTask
            onAdd={async (title) => {
              await createTask({ title, project: address, workspace, status: 'todo', ...(milestone ? { milestone } : {}) }, workspace)
              list.reload()
              onChanged()
            }}
          />
        )}
      </Section>
    </>
  )
}

function AddTask({ onAdd }: { onAdd: (title: string) => Promise<void> }): JSX.Element {
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  return (
    <form
      className="mt-2 flex gap-2"
      onSubmit={(e) => {
        e.preventDefault()
        if (!title.trim() || busy) return
        setBusy(true)
        setError(null)
        onAdd(title.trim())
          .then(() => setTitle(''))
          .catch((err: unknown) => setError(errorText(err, 'Could not add the task')))
          .finally(() => setBusy(false))
      }}
    >
      <input
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Add a task…"
        aria-label="New task"
        className="min-w-0 flex-1 rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground placeholder:text-placeholder"
      />
      <Button size="sm" variant="outline" type="submit" disabled={busy || !title.trim()}>
        Add
      </Button>
      {error && <span className="self-center text-[12px] text-destructive">{error}</span>}
    </form>
  )
}

function MilestonesSection({
  milestones,
  editable,
  workspace,
  handle,
  selected,
  onSelect,
  onChanged,
}: {
  milestones: readonly Milestone[]
  editable: boolean
  workspace: string
  handle: string
  selected: string | null
  onSelect: (name: string | null) => void
  onChanged: () => void
}): JSX.Element {
  const [error, setError] = useState<string | null>(null)
  const run = (p: Promise<unknown>) => {
    setError(null)
    p.then(onChanged).catch((err: unknown) => setError(errorText(err, 'Could not save the milestone')))
  }
  return (
    <Section id="project-milestones" title="Milestones" description="The stops on the way. Pick one to see only its tasks below.">
      {milestones.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No milestones.</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border bg-card" data-testid="milestone-table">
          <table className="w-full border-collapse text-[13px]">
            <thead className="border-b border-border">
              <tr className="text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                <th className="px-3 py-1.5 font-medium">Milestone</th>
                <th className="px-2 py-1.5 font-medium">Progress</th>
                <th className="px-2 py-1.5 font-medium">Target</th>
                {editable && <th className="w-16 px-2 py-1.5" aria-label="Actions" />}
              </tr>
            </thead>
            <tbody>
              {milestones.map((m) => (
                <MilestoneRow
                  key={m.id}
                  milestone={m}
                  editable={editable}
                  selected={selected === m.name}
                  onSelect={() => onSelect(selected === m.name ? null : m.name)}
                  onRename={(name) => run(updateMilestone(workspace, handle, m.id, { name }))}
                  onDate={(target_date) => run(updateMilestone(workspace, handle, m.id, { target_date }))}
                  onDelete={() => {
                    if (selected === m.name) onSelect(null)
                    run(deleteMilestone(workspace, handle, m.id))
                  }}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {error && <p className="mt-2 text-[12px] text-destructive">{error}</p>}
      {editable && <AddMilestone onAdd={(name, target_date) => run(createMilestone(workspace, handle, { name, target_date }))} />}
    </Section>
  )
}

function MilestoneRow({
  milestone: m,
  editable,
  selected,
  onSelect,
  onRename,
  onDate,
  onDelete,
}: {
  milestone: Milestone
  editable: boolean
  selected: boolean
  onSelect: () => void
  onRename: (name: string) => void
  onDate: (d: string | null) => void
  onDelete: () => void
}): JSX.Element {
  const [renaming, setRenaming] = useState(false)
  const [draft, setDraft] = useState(m.name)
  return (
    <tr className={`border-b border-border last:border-0 hover:bg-muted/50 ${selected ? 'bg-primary/5' : ''}`} data-milestone={m.name}>
      <td className="px-3 py-1.5">
        {renaming ? (
          <input
            autoFocus
            value={draft}
            aria-label="Milestone name"
            onChange={(e) => setDraft(e.target.value)}
            onBlur={() => {
              setRenaming(false)
              if (draft.trim() && draft.trim() !== m.name) onRename(draft.trim())
              else setDraft(m.name)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
              if (e.key === 'Escape') {
                setDraft(m.name)
                setRenaming(false)
              }
            }}
            className="w-full rounded border border-input bg-input px-1 text-[13px] text-foreground"
          />
        ) : (
          <button type="button" onClick={onSelect} aria-pressed={selected} className="text-left font-medium text-foreground hover:text-primary">
            {m.name}
          </button>
        )}
        {m.description && <div className="text-[12px] text-muted-foreground">{m.description}</div>}
      </td>
      <td className="px-2 py-1.5">
        <ProgressBar done={m.done} total={m.total} />
      </td>
      <td className="px-2 py-1.5">
        <DateField label={`${m.name} target date`} value={m.target_date} disabled={!editable} onChange={onDate} />
      </td>
      {editable && (
        <td className="px-2 py-1.5 text-right">
          <span className="inline-flex gap-1">
            <button type="button" aria-label={`Rename ${m.name}`} onClick={() => setRenaming(true)} className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground">
              <Pencil aria-hidden="true" className="size-3.5" />
            </button>
            <button type="button" aria-label={`Delete ${m.name}`} onClick={onDelete} className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-destructive">
              <Trash2 aria-hidden="true" className="size-3.5" />
            </button>
          </span>
        </td>
      )}
    </tr>
  )
}

function AddMilestone({ onAdd }: { onAdd: (name: string, target: string | null) => void }): JSX.Element {
  const [name, setName] = useState('')
  const [target, setTarget] = useState('')
  return (
    <form
      className="mt-2 flex flex-wrap gap-2"
      data-testid="project-milestones-form"
      onSubmit={(e) => {
        e.preventDefault()
        if (!name.trim()) return
        onAdd(name.trim(), target || null)
        setName('')
        setTarget('')
      }}
    >
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Add a milestone…"
        aria-label="New milestone"
        className="min-w-0 flex-1 rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground placeholder:text-placeholder"
      />
      <input
        type="date"
        value={target}
        onChange={(e) => setTarget(e.target.value)}
        aria-label="New milestone target date"
        className="rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground"
      />
      <Button size="sm" variant="outline" type="submit" disabled={!name.trim()}>
        Add
      </Button>
    </form>
  )
}
