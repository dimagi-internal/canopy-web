// /w/:workspace/projects/:handle — a project's ONE page, for teammates and
// editors alike. Its properties (status, health, lead, agents, dates, its main
// screen) and its main button; then only the sections that hold something —
// views, the latest update, milestones, scope against done, its tasks (with its
// repeating ones grouped apart), meetings, toolbox, people, shares, where it
// stands, ideas, check-ins and the turns that touched it. An empty section is a
// line in the Add… menu until someone opens it; a project that is mostly tasks
// shows them first. The controls a viewer may not use (`may_edit`) are not drawn.
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ArrowRight, Plus, X } from 'lucide-react'
import { Button } from 'canopy-ui/ui'
import { WorkbenchSkeleton } from 'canopy-ui'
import { getProjectByAddress, updateProject, type ProjectDetail, type ProjectPatch } from '@/api/work'
import { ProjectViewsSection } from '@/dynamicViews/ProjectViewsSection'
import { Section } from '@/components/objectPage/settingsLayout'
import { ProposalCard } from '@/components/projects/ProposalCard'
import { useEngagementView } from '@/components/projects/useEngagementView'
import { ProgressBar } from '@/components/work/projectBits'
import { ScopeChart, scopePoints } from '@/components/work/ScopeChart'
import { TasksView } from '@/components/work/TasksView'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'
import { CheckinTimeline } from '@/pages/agents/CheckinParts'
import { CheckinsPanel } from '@/pages/agents/ProjectCheckins'
import { ProjectOverview, type OverviewPart } from '@/pages/agents/ProjectOverview'
import { errorText } from '@/api/problem'
import { MilestonesSection } from '@/pages/work/ProjectMilestones'
import { PropertyHeader, primaryLink } from '@/pages/work/ProjectProperties'
import { ProjectMeetings } from '@/pages/work/ProjectMeetings'
import { ProjectFiles } from '@/pages/work/ProjectFiles'
import { ProjectServices } from '@/pages/work/ProjectServices'
import { UpdatesSection } from '@/pages/work/ProjectUpdates'
import { ProjectMembers } from '@/pages/work/ProjectMembers'
import { ProjectShares } from '@/pages/work/ProjectShares'
import { TASK_RESOURCE, useResource } from '@/widget/useResource'
import { CanopyAIButton } from '@/widget/CanopyAIButton'
import { useInlineLauncher } from '@/widget/assistant'

/** How long a burst of task changes waits before the page's counts re-read. */
const REFRESH_DEBOUNCE_MS = 600

export function ProjectPage(): JSX.Element {
  const { workspace = '', handle = '' } = useParams()
  const address = `${workspace}/${handle}`
  const [state, setState] = useState<{ key: string; detail: ProjectDetail | null; error: string | null } | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [timelineVersion, setTimelineVersion] = useState(0)
  const [milestone, setMilestone] = useState<string | null>(null)
  // What each self-loading section says it holds, and the ones the viewer opened from Add….
  const [reported, setReported] = useState<Partial<Record<ReportedId, boolean>>>({})
  const [opened, setOpened] = useState<Set<SlotId>>(() => new Set())
  const report = useCallback(
    (id: ReportedId, has: boolean) => setReported((cur) => (cur[id] === has ? cur : { ...cur, [id]: has })),
    [],
  )
  const reporter = useMemo(
    () => ({
      views: (has: boolean) => report('views', has),
      meetings: (has: boolean) => report('meetings', has),
      members: (has: boolean) => report('members', has),
      shares: (has: boolean) => report('shares', has),
      checkins: (has: boolean) => report('checkins', has),
      timeline: (has: boolean) => report('timeline', has),
    }),
    [report],
  )
  useInlineLauncher()

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
  const checkinKey = { agent: lead, ref: agentRef, workspace: detail.workspace || workspace }
  const primary = primaryLink(detail)

  // ---- sections on demand ----
  // A section shows when it has something in it, or when the viewer just chose it from
  // Add…; an empty one is a line in that menu, not a paragraph and a form.
  const known: Partial<Record<SlotId, boolean>> = {
    proposal: Boolean(detail.proposal),
    proposed: (detail.proposed_tasks ?? []).length > 0,
    update: Boolean(detail.latest_update) || (detail.draft_updates ?? []).length > 0,
    milestones: (detail.milestones ?? []).length > 0,
    scope: (detail.progress?.total ?? 0) > 0,
    tasks: true,
    toolbox: (detail.services ?? []).length > 0 || (detail.vaults ?? []).length > 0,
    turns: turns.length > 0,
  }
  const visible = (id: SlotId) => Boolean(known[id] ?? reported[id as ReportedId]) || opened.has(id)
  // Mostly tasks (no update, no milestones yet): the tasks come first.
  const tasksFirst = !known.update && !known.milestones
  const order = sectionOrder(tasksFirst)
  const firstShown = order.find(visible) ?? null
  const slot = (id: SlotId, node: ReactNode) => (
    <div
      key={id}
      data-slot={id}
      className={!visible(id) ? 'hidden' : id === firstShown ? undefined : 'border-t border-border pt-6'}
    >
      {node}
    </div>
  )
  const addable = ADDABLE.filter(
    (a) =>
      !visible(a.id) &&
      (a.id === 'ideas' || detail.may_edit) &&
      (!(a.id === 'members' || a.id === 'shares') || detail.visibility !== 'personal') &&
      (!(a.id === 'ideas' || a.id === 'checkins') || Boolean(lead && agentRef)),
  )
  const reveal = (a: Addable) => {
    setOpened((cur) => new Set(cur).add(a.id))
    // After it is drawn: bring it to the reader.
    setTimeout(() => document.getElementById(a.anchor)?.scrollIntoView?.({ behavior: 'smooth', block: 'start' }), 50)
  }

  const nodes: Partial<Record<SlotId, ReactNode>> = {
    proposal: detail.proposal && (
      <Section id="project-proposal" title="Proposal" description="This project was proposed. Start it or decline it here.">
        <ProposalCard proposal={detail.proposal} onChanged={reload} />
      </Section>
    ),
    proposed: (detail.proposed_tasks ?? []).length > 0 && (
      <Section id="project-proposed" title="Proposed tasks" description="Tasks a check-in suggested. Start or decline each one.">
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
    ),
    views: (
      <ProjectViewsSection workspace={workspace} handle={handle} projectName={detail.name} onContent={reporter.views} />
    ),
    update: <UpdatesSection detail={detail} workspace={workspace} handle={handle} onChanged={reload} />,
    milestones: (
      <MilestonesSection
        milestones={detail.milestones ?? []}
        editable={detail.may_edit}
        workspace={workspace}
        handle={handle}
        selected={milestone}
        onSelect={setMilestone}
        onChanged={reload}
      />
    ),
    scope: <ScopeSection detail={detail} />,
    tasks: (
      <ProjectTasks
        detail={detail}
        workspace={workspace}
        address={address}
        milestone={milestone}
        onClearMilestone={() => setMilestone(null)}
        onChanged={reload}
      />
    ),
    meetings: (
      <ProjectMeetings
        detail={detail}
        workspace={workspace}
        handle={handle}
        onSaveInstructions={(text) => void save({ meeting_instructions: text })}
        onContent={reporter.meetings}
      />
    ),
    toolbox: (
      <>
        <ProjectServices detail={detail} workspace={workspace} handle={handle} onChanged={reload} />
        <ProjectFiles workspace={workspace} handle={handle} />
      </>
    ),
    members: detail.visibility !== 'personal' && (
      <ProjectMembers detail={detail} workspace={workspace} handle={handle} onChanged={reload} onContent={reporter.members} />
    ),
    shares: detail.visibility !== 'personal' && (
      <ProjectShares detail={detail} workspace={workspace} handle={handle} onChanged={reload} onContent={reporter.shares} />
    ),
    // The overview draws three sections (where it stands, ideas, people); each is its own slot.
    standing: lead && agentRef && (
      <ProjectOverview
        key="overview"
        agent={lead}
        projectRef={agentRef}
        pageHref={(personId) =>
          scopedPath(workspace, `/agents/${lead}/projects/${encodeURIComponent(agentRef)}/people/${personId}`)
        }
        showRefs={detail.may_edit}
        bare
        wrap={(part, has, node) => <OverviewSlot part={part} has={has} report={report} render={slot} node={node} />}
      />
    ),
    checkins: lead && agentRef && (
      <Section id="project-checkins" title="Check-ins" description="Regular meetings an agent runs for this project.">
        <CheckinsPanel
          project={checkinKey}
          projectAgent={lead}
          canAdd={detail.may_edit}
          onRan={() => setTimelineVersion((v) => v + 1)}
          onContent={reporter.checkins}
        />
      </Section>
    ),
    timeline: lead && agentRef && (
      <Section id="project-timeline" title="Timeline" description="Each check-in that ran, newest first.">
        <CheckinTimeline project={checkinKey} version={timelineVersion} onContent={reporter.timeline} />
      </Section>
    ),
    turns: (
      <Section id="project-activity" title="Recent turns" description="Agent work on this project's tasks, newest first.">
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
      </Section>
    ),
  }

  const actions = (
    <div className="mt-2 flex flex-wrap items-center gap-2" data-testid="project-actions">
      {primary && (
        <Link
          to={primary.url}
          data-testid="project-primary"
          className="inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-[13px] font-medium text-primary-foreground hover:bg-primary/90"
        >
          Open {primary.label}
          <ArrowRight aria-hidden="true" className="size-4" />
        </Link>
      )}
      {addable.length > 0 && <AddMenu items={addable} onPick={reveal} />}
      <span className="sm:hidden">
        <CanopyAIButton />
      </span>
    </div>
  )

  return (
    // pb-28: room under the last section for the site-wide floating "Canopy AI" button
    // (on a phone the page carries its own button instead: `useInlineLauncher`).
    <div className="max-w-5xl pb-28" data-testid="project-page">
      {back}
      <PropertyHeader detail={detail} workspace={workspace} onSave={save} actions={actions} />
      {saveError && (
        <p role="alert" className="mb-4 rounded border border-destructive/30 bg-destructive/10 px-3 py-2 text-[12px] text-destructive">
          {saveError}
        </p>
      )}
      {/* Ideas and people are drawn by the overview, in the standing slot. */}
      {order.filter((id) => id !== 'ideas' && id !== 'people').map((id) =>
        id === 'standing' ? <Fragment key="overview">{nodes.standing}</Fragment> : slot(id, nodes[id]),
      )}
    </div>
  )
}

// ---- sections on demand ------------------------------------------------------------

type SlotId =
  | 'proposal' | 'proposed' | 'views' | 'update' | 'milestones' | 'scope' | 'tasks' | 'meetings' | 'toolbox'
  | 'members' | 'shares' | 'standing' | 'ideas' | 'people' | 'checkins' | 'timeline' | 'turns'

/** The sections whose content only their own component knows (it reports it). */
type ReportedId = 'views' | 'meetings' | 'members' | 'shares' | 'standing' | 'ideas' | 'people' | 'checkins' | 'timeline'

const REST: SlotId[] = ['meetings', 'toolbox', 'members', 'shares', 'standing', 'ideas', 'people', 'checkins', 'timeline', 'turns']

/** The page's order. A project that is mostly tasks shows them right under its decisions. */
export function sectionOrder(tasksFirst: boolean): SlotId[] {
  return tasksFirst
    ? ['proposal', 'proposed', 'tasks', 'views', 'update', 'milestones', 'scope', ...REST]
    : ['proposal', 'proposed', 'views', 'update', 'milestones', 'scope', 'tasks', ...REST]
}

interface Addable {
  id: SlotId
  label: string
  /** The section's own id, to scroll to. */
  anchor: string
}

/** What Add… offers: an empty section to open, by what you would add to it. */
const ADDABLE: Addable[] = [
  { id: 'update', label: 'Update', anchor: 'project-update' },
  { id: 'milestones', label: 'Milestone', anchor: 'project-milestones' },
  { id: 'meetings', label: 'Meeting', anchor: 'project-meetings' },
  { id: 'checkins', label: 'Check-in', anchor: 'project-checkins' },
  { id: 'ideas', label: 'Idea', anchor: 'team-ideas' },
  { id: 'toolbox', label: 'Toolbox item', anchor: 'project-services' },
  { id: 'members', label: 'Person', anchor: 'project-members' },
  { id: 'shares', label: 'Share', anchor: 'project-shares' },
  { id: 'views', label: 'View', anchor: 'project-views' },
]

/** One button, one short list: the sections this project does not show yet. */
function AddMenu({ items, onPick }: { items: Addable[]; onPick: (a: Addable) => void }): JSX.Element {
  const [open, setOpen] = useState(false)
  return (
    <div className="relative">
      <Button size="sm" variant="outline" aria-expanded={open} onClick={() => setOpen((on) => !on)} data-testid="project-add">
        <Plus aria-hidden="true" />
        Add…
      </Button>
      {open && (
        <ul
          role="menu"
          aria-label="Add to this project"
          className="absolute left-0 z-20 mt-1 min-w-40 rounded-md border border-border bg-popover p-1 text-[13px] shadow-md"
        >
          {items.map((a) => (
            <li key={a.id} role="none">
              <button
                type="button"
                role="menuitem"
                data-testid={`add-${a.id}`}
                onClick={() => {
                  setOpen(false)
                  onPick(a)
                }}
                className="w-full rounded px-2 py-1.5 text-left text-foreground hover:bg-muted"
              >
                {a.label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** One of the overview's sections, in its slot: it reports whether it has anything. */
function OverviewSlot({
  part,
  has,
  report,
  render,
  node,
}: {
  part: OverviewPart
  has: boolean
  report: (id: ReportedId, has: boolean) => void
  render: (id: SlotId, node: ReactNode) => ReactNode
  node: ReactNode
}): ReactNode {
  useEffect(() => report(part, has), [part, has, report])
  return render(part, node)
}

// ---- milestones, scope, tasks ------------------------------------------------------

function ScopeSection({ detail }: { detail: ProjectDetail }): JSX.Element {
  const progress = detail.progress
  return (
    <Section id="project-scope" title="Scope and done" description="Tasks done against tasks planned, week by week. Repeating tasks are not counted.">
      {progress && (
        <p className="mb-2 flex flex-wrap items-center gap-3 text-[12px] text-muted-foreground" data-testid="project-progress">
          <ProgressBar done={progress.done} total={progress.total} />
          <span>
            {progress.done} of {progress.total} done · {progress.started} in progress
          </span>
        </p>
      )}
      <ScopeChart points={scopePoints(progress?.scope_history)} />
    </Section>
  )
}

/** The project's tasks: the workspace's Tasks list pinned to it (and to a chosen milestone),
 *  its work first and its repeating templates in their own group. */
function ProjectTasks({
  detail,
  workspace,
  address,
  milestone,
  onClearMilestone,
  onChanged,
}: {
  detail: ProjectDetail
  workspace: string
  address: string
  milestone: string | null
  onClearMilestone: () => void
  onChanged: () => void
}): JSX.Element {
  // The project (and a chosen milestone) pinned; every other filter is the list's own.
  const fixed = useMemo(
    () => ({ project: address, recurring: 'false' as const, ...(milestone ? { milestone } : {}) }),
    [address, milestone],
  )
  return (
    <Section id="project-tasks" title="Tasks">
      {milestone && (
        <div className="mb-2 text-[12px]">
          <span className="inline-flex items-center gap-1 rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-primary" data-testid="milestone-filter">
            Milestone: {milestone}
            <button type="button" aria-label="Show every milestone" onClick={onClearMilestone} className="hover:text-foreground">
              <X aria-hidden="true" className="size-3" />
            </button>
          </span>
        </div>
      )}
      <TasksView workspace={workspace} fixed={fixed} nest repeating canCreate={detail.may_edit} onChanged={onChanged} />
    </Section>
  )
}
