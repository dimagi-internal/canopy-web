import { useMemo, useRef, useState, type JSX } from 'react'
import { Link, useInRouterContext, useParams } from 'react-router-dom'
import {
  actOnTask,
  AgentApiError,
  type TaskAction,
  type TaskActionOut,
  type TaskOut,
} from '@/api/agents'
import { Markdown } from '@/components/Markdown'
import { TaskAge } from '@/components/TaskAge'

// ── "Who has the ball" model ───────────────────────────────────────────────
// The board is organized by whose court the next action sits in, not by equal
// status columns. `assigned` names who the next step waits on: the agent or a
// human. Empty, or the board's own agent slug (any case), means the agent —
// every agent's board renders this, so 'eva' on Eva's board is Eva.

function isAgent(task: TaskOut): boolean {
  const a = (task.assigned || '').trim().toLowerCase()
  return a === '' || a === (task.agent_slug || '').toLowerCase()
}

function headline(task: TaskOut): string {
  return (task.next_action || '').trim() || (task.title || '').trim()
}

function formatDue(s: string): string {
  return new Date(s).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
  })
}

// A short, human "when" for action timestamps: "Jun 17, 2:30 PM".
function formatWhen(s: string): string {
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

// An action read as a past-tense verb for activity rows.
const ACTION_VERB: Record<string, string> = {
  approve: 'approved',
  decline: 'declined',
  reply: 'replied to',
  nudge: 'nudged',
  done: 'completed',
}

function actionVerb(action: string): string {
  return ACTION_VERB[action] ?? action
}

function isPastDue(due: string): boolean {
  const d = new Date(due)
  if (Number.isNaN(d.getTime())) return false
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return d.getTime() < today.getTime()
}

/** `jarvis` → `Jarvis`. Slugs are lowercase `[a-z0-9-]`, so capitalising the
 *  first letter is the agent's display name in every case the fleet has. */
// eslint-disable-next-line react-refresh/only-export-components -- tested pure helper beside its only caller
export function agentDisplayName(slug: string): string {
  return slug ? slug.charAt(0).toUpperCase() + slug.slice(1) : 'Agent'
}

// ── The five actions ─────────────────────────────────────────────────────────
// A card offers exactly the actions that apply (spec § "Tasks page"):
//   any live task          → reply
//   open review            → + approve · decline
//   open question          → reply is the answer · + decline
//   suggested, asks nothing → + approve · decline (a suggestion IS the
//                             question "should I do this?")
//   ask closed             → reply only (approve/decline would 409)
//   editors, in progress   → + nudge
//   editors, live          → + done
//   done/declined          → nothing
// An action that hands the agent work STARTS its turn (Jonathan, 2026-10-08):
// approve always does, for anyone; nudge does (editors); an editor's reply does,
// while a viewer's reply is a note the agent reads on its next turn — the copy
// says which. Viewers may approve/decline/reply; nudge/done need edit (403
// otherwise), so they are not offered to a viewer at all. Decline takes the text
// in the reply box, when there is any, as its reason.

function isLive(task: TaskOut): boolean {
  return task.status === 'suggested' || task.status === 'in_progress'
}

// eslint-disable-next-line react-refresh/only-export-components -- the card's action rule, exported for its tests
export function availableActions(task: TaskOut, canEdit: boolean): TaskAction[] {
  if (!isLive(task)) return []
  const kind = (task.ask_kind || '').trim()
  let actions: TaskAction[] = ['reply']
  // A CLOSED ask is not "no ask": the server refuses approve/decline on it
  // (ClosedAskError, 409) — e.g. a suggested question someone already answered.
  if (task.ask_open && kind === 'review') actions = [...actions, 'approve', 'decline']
  else if (task.ask_open && kind === 'question') actions = [...actions, 'decline']
  else if (!kind && task.status === 'suggested') actions = [...actions, 'approve', 'decline']
  // Nudge re-starts work already under way; a suggested task is started by
  // approving it (the server refuses a nudge on one, 409).
  if (canEdit && task.status === 'in_progress') actions = [...actions, 'nudge']
  if (canEdit) actions = [...actions, 'done']
  return actions
}

// ── Small primitives ────────────────────────────────────────────────────────

// confidence: 'high' = solid dot, 'low' = hollow/hatched, '' = nothing.
function ConfidenceDot({ confidence }: { confidence: string }): JSX.Element | null {
  const c = (confidence || '').trim().toLowerCase()
  if (c === 'high') {
    return (
      <span
        className="h-2 w-2 shrink-0 rounded-full bg-primary"
        title="High confidence"
        aria-label="High confidence"
      />
    )
  }
  if (c === 'low') {
    return (
      <span
        className="h-2 w-2 shrink-0 rounded-full border border-dashed border-muted-foreground/70"
        title="Low confidence"
        aria-label="Low confidence"
      />
    )
  }
  return null
}

// The "ball is in the agent's court" affordance: the agent's name + a pulsing dot.
function EchoWorking({ name }: { name: string }): JSX.Element {
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-primary">
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary/60" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-primary" />
      </span>
      {name}
    </span>
  )
}

// The "ball is in a human's court" affordance: an amber waiting chip.
function WaitingChip({ who }: { who: string }): JSX.Element {
  return (
    <span className="inline-flex items-center gap-1 rounded border border-warning/30 bg-warning/15 px-1.5 py-0.5 text-[11px] font-medium text-warning">
      Waiting on {who}
    </span>
  )
}

function OwnerTag({ owner }: { owner: string }): JSX.Element | null {
  const o = (owner || '').trim()
  if (!o) return null
  return (
    <span className="text-[10px] text-muted-foreground" title={`Owner: ${o}`}>
      Owner · {o}
    </span>
  )
}

function DueChip({ due, done }: { due: string; done: boolean }): JSX.Element {
  const pastDue = !done && isPastDue(due)
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] ${
        pastDue
          ? 'border-destructive/40 bg-destructive/10 text-destructive'
          : 'border-border bg-muted text-muted-foreground'
      }`}
      title={pastDue ? 'Past due' : undefined}
    >
      {pastDue && <span aria-hidden>⚠</span>}
      {formatDue(due)}
    </span>
  )
}

function TaskLinkChip({ label, url }: { label: string; url: string }): JSX.Element | null {
  if (!url) return null
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1 rounded border border-border bg-muted px-2 py-0.5 text-[10px] font-medium text-muted-foreground transition-colors hover:border-primary/50 hover:text-primary"
    >
      <span className="text-primary/70">↗</span>
      {label || url}
    </a>
  )
}

// The task's project, as a link to that project's page — or a muted "No
// project" pill, so a loose task reads as loose rather than as missing data
// (Jonathan, 2026-10-08: "tasks need a more clear link back to their project,
// or a pill that says no-project"). The route is the agent's project page,
// `/w/:workspace/agents/:slug/projects/:ref`. Off a workspace route (the fleet
// queue at /supervisor) the caller names the task's workspace; failing that,
// `/agents/*` resolves the active workspace (TenantRedirect).
// eslint-disable-next-line react-refresh/only-export-components -- tested pure helper beside its only caller
export function projectHref(task: TaskOut, workspace?: string): string {
  const tail = `agents/${task.agent_slug}/projects/${task.project_ext_id}`
  return workspace ? `/w/${workspace}/${tail}` : `/${tail}`
}

const PILL =
  'inline-flex max-w-full items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium'

function ProjectPill({ task, workspace }: { task: TaskOut; workspace?: string }): JSX.Element {
  const inRouter = useInRouterContext()
  const ref = (task.project_ext_id || '').trim()
  if (!ref) {
    return (
      <span
        className={`${PILL} border-border bg-muted text-muted-foreground`}
        data-testid={`task-project-${task.ext_id}`}
      >
        No project
      </span>
    )
  }
  const name = (task.project_name || '').trim()
  const href = projectHref(task, workspace)
  const className = `${PILL} border-primary/30 bg-primary/10 text-primary transition-colors hover:border-primary/60 hover:bg-primary/15`
  const body = (
    <>
      <span className="shrink-0 text-primary/70">{ref}</span>
      {name && <span className="truncate">{name}</span>}
    </>
  )
  const title = name ? `Project ${ref} · ${name}` : `Project ${ref}`
  const testId = `task-project-${task.ext_id}`
  // A plain anchor outside a router (a card rendered on its own) still navigates.
  return inRouter ? (
    <Link to={href} className={className} title={title} data-testid={testId}>
      {body}
    </Link>
  ) : (
    <a href={href} className={className} title={title} data-testid={testId}>
      {body}
    </a>
  )
}

// A "source ↗" chip linking the originating thread/doc.
function SourceChip({ url }: { url: string }): JSX.Element | null {
  if (!url || !url.trim()) return null
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1 rounded border border-border bg-muted px-2 py-0.5 text-[10px] font-medium text-muted-foreground transition-colors hover:border-primary/50 hover:text-primary"
      title={url}
    >
      source <span className="text-primary/70">↗</span>
    </a>
  )
}

// A muted "Why: …" rationale line — expandable when long. Lets a human validate
// a suggested task before acting on it. When `sourceUrl` is set, the grounded
// source the agent read is linked inline right after the rationale — the trust signal.
function RationaleLine({
  rationale,
  sourceUrl,
}: {
  rationale: string
  sourceUrl?: string
}): JSX.Element | null {
  const text = (rationale || '').trim()
  const source = (sourceUrl || '').trim()
  const [expanded, setExpanded] = useState(false)
  if (!text) return null
  const long = text.length > 120
  return (
    <p className="mt-1.5 text-[11px] leading-snug text-muted-foreground/90">
      <span className="text-muted-foreground/70">Why: </span>
      {long && !expanded ? `${text.slice(0, 120).trimEnd()}…` : text}
      {long && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="ml-1 text-primary/80 hover:text-primary"
        >
          {expanded ? 'less' : 'more'}
        </button>
      )}
      {source && (
        <a
          href={source}
          target="_blank"
          rel="noreferrer"
          className="ml-1.5 whitespace-nowrap font-medium text-primary/80 hover:text-primary"
          title={source}
        >
          source <span className="text-primary/70">↗</span>
        </a>
      )}
    </p>
  )
}

// The outcome of the most recent action the agent applied to this task —
// surfaces `result_note` + `applied_at`.
function LastActivityLine({ action }: { action: TaskActionOut }): JSX.Element {
  const note = (action.result_note || '').trim() || actionVerb(action.action)
  const when = action.applied_at ? formatWhen(action.applied_at) : ''
  return (
    <p
      className="mt-1.5 text-[11px] leading-snug text-muted-foreground/90"
      data-testid="task-last-activity"
    >
      <span className="text-muted-foreground/70">last: </span>
      {note}
      {when && <span className="text-muted-foreground/60"> · {when}</span>}
    </p>
  )
}

/** Where approving (or answering) sends the work — the one place a cross-agent
 *  fan-out is visible on the card. An empty target means the task's own agent. */
function onApproveHint(task: TaskOut): string {
  const targets = (task.on_approve ?? [])
    .map((d) => (d as { target_agent?: string }).target_agent || task.agent_slug)
    .filter((v, i, a) => a.indexOf(v) === i)
  return targets.length ? `runs on ${targets.join(', ')}` : ''
}

/** Who starts when the task is approved: its `on_approve` targets, else its own agent. */
function startsOnApprove(task: TaskOut): string {
  const targets = (task.on_approve ?? [])
    .map((d) => (d as { target_agent?: string }).target_agent || task.agent_slug)
    .filter((v, i, a) => a.indexOf(v) === i)
  return (targets.length ? targets : [task.agent_slug]).map(agentDisplayName).join(', ')
}

// ── The action row ──────────────────────────────────────────────────────────
//
// `min-h-11 sm:min-h-0` on every control: measured on a Pixel 7 against the
// deployed app, the old decision row rendered 25-27px tall — well under the 44px
// both Apple and Google publish as the minimum. Desktop keeps the compact size
// from `sm:` up.
const BTN =
  'min-h-11 sm:min-h-0 rounded-md border border-border px-3 py-1 text-[12px] text-foreground transition-colors hover:bg-muted disabled:opacity-50'
const BTN_QUIET =
  'min-h-11 sm:min-h-0 rounded-md px-3 py-1 text-[12px] text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50'

/** The reply box's hint says what sending does: an editor's reply starts the
 *  agent now, a viewer's waits for the agent's next turn. With Approve or
 *  Decline on the card, the text also rides along with either as its note. */
function replyPlaceholder(task: TaskOut, canEdit: boolean, isQuestion: boolean): string {
  if (isQuestion) return 'Type an answer…'
  const name = agentDisplayName(task.agent_slug)
  return canEdit ? `Reply — ${name} picks it up now…` : `Leave ${name} a note for its next turn…`
}

function errorText(e: unknown): string {
  if (e instanceof AgentApiError && e.detail) return e.detail
  if (e instanceof AgentApiError && e.status === 409) return 'Someone already acted on this.'
  return e instanceof Error ? e.message : 'Action failed'
}

function TaskActions({
  task,
  canEdit,
  onChanged,
}: {
  task: TaskOut
  canEdit: boolean
  onChanged?: () => void
}): JSX.Element | null {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reply, setReply] = useState('')
  const inFlight = useRef(false)

  const actions = availableActions(task, canEdit)
  if (actions.length === 0) return null
  const has = (a: TaskAction) => actions.includes(a)

  const runs = (task.on_approve ?? []).length > 0
  const isQuestion = task.ask_open && (task.ask_kind || '').trim() === 'question'
  const replyLabel = isQuestion ? (runs ? 'Answer & run' : 'Answer') : 'Reply'
  // Every agent's board renders this card, so the name comes from the task.
  const name = agentDisplayName(task.agent_slug)

  async function run(action: TaskAction, comment?: string) {
    // A ref, not `busy`: state is stale inside a double-click's second handler.
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError(null)
    try {
      await actOnTask(task.agent_slug, task.ext_id, action, comment)
      setReply('')
      onChanged?.()
    } catch (e) {
      setError(errorText(e))
      // 409: the ask was closed elsewhere — the card is stale, so refetch it.
      if (e instanceof AgentApiError && e.status === 409) onChanged?.()
    } finally {
      // The card stays mounted (keyed by ext_id) after a reply/nudge, so it
      // must come back enabled — a second reply is a normal thing to do.
      inFlight.current = false
      setBusy(false)
    }
  }

  return (
    <div className="mt-2.5 border-t border-border/60 pt-2">
      {has('reply') && (
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="text"
            value={reply}
            disabled={busy}
            placeholder={replyPlaceholder(task, canEdit, isQuestion)}
            aria-label={isQuestion ? 'Answer' : 'Reply'}
            onChange={(e) => setReply(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && reply.trim()) run('reply', reply.trim())
            }}
            // `basis-full` gives the field the whole row on a phone and pushes the
            // buttons below; from `sm` up they share a line.
            className="min-h-11 w-full min-w-0 basis-full rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground placeholder:text-muted-foreground disabled:opacity-50 sm:min-h-0 sm:w-auto sm:flex-1 sm:basis-auto dark:placeholder:text-foreground-secondary"
            data-testid={`task-reply-${task.ext_id}`}
          />
          <button
            type="button"
            disabled={busy || !reply.trim()}
            onClick={() => run('reply', reply.trim())}
            className={BTN}
          >
            {replyLabel}
          </button>
        </div>
      )}

      {(has('approve') || has('decline') || has('nudge') || has('done')) && (
        <div className={`flex flex-wrap items-center gap-2 ${has('reply') ? 'mt-2' : ''}`}>
          {has('approve') && (
            <button
              type="button"
              disabled={busy}
              // Like Decline's reason: anything typed above rides along with the approval.
              onClick={() => run('approve', reply.trim() || undefined)}
              title="Approve — anything typed above goes with it"
              className={BTN}
            >
              Approve — {startsOnApprove(task)} starts now
            </button>
          )}
          {has('decline') && (
            <button
              type="button"
              disabled={busy}
              // The reply box doubles as the optional reason: no second field.
              onClick={() => run('decline', reply.trim() || undefined)}
              title="Decline — anything typed above is sent as the reason"
              className={BTN_QUIET}
            >
              Decline
            </button>
          )}
          {has('nudge') && (
            <button
              type="button"
              disabled={busy}
              onClick={() => run('nudge')}
              title={`Start a ${name} turn on this now — the status stays as it is`}
              className={BTN}
            >
              Nudge {name}
            </button>
          )}
          {has('done') && (
            <button type="button" disabled={busy} onClick={() => run('done')} className={BTN_QUIET}>
              Mark done
            </button>
          )}
        </div>
      )}

      {error && <p className="mt-1.5 text-[11px] text-destructive">{error}</p>}
    </div>
  )
}

// ── Card ────────────────────────────────────────────────────────────────────

// THE card. Every surface that shows a task — the agent board, the fleet
// "Waiting on you" queue, a project page — renders this one, so an ask is
// decidable in place wherever the task appears. (ItemCard, its Inbox-only twin,
// is gone: an item stopped being its own model in #871/#873.)
export function TaskCard({
  task,
  onChanged,
  canEdit,
  showAgent = false,
  showProject = true,
  workspace,
  lastApplied,
}: {
  task: TaskOut
  onChanged?: () => void
  canEdit: boolean
  /** Tag the card with its agent — for fleet-wide surfaces. */
  showAgent?: boolean
  /** Show the task's project link (or "No project"). Off on a project's own
   *  page, where every card is in that project. */
  showProject?: boolean
  /** The task's workspace, for the project link — needed only off a
   *  `/w/:workspace/…` route (the fleet queue). */
  workspace?: string
  /** The latest applied action on this task, when the caller has it. */
  lastApplied?: TaskActionOut
}): JSX.Element {
  const { workspace: routeWorkspace } = useParams()
  const head = headline(task)
  const outcome = (task.title || '').trim()
  const showOutcome = outcome && outcome !== head
  const echo = isAgent(task)
  const isSuggested = task.status === 'suggested'
  const isDone = task.status === 'done'
  const hasRationale = Boolean((task.rationale || '').trim())
  const askKind = (task.ask_kind || '').trim()
  const askOpen = Boolean(askKind) && task.ask_open
  const meta = [showAgent ? task.agent_slug : '', askOpen ? onApproveHint(task) : '']
    .filter(Boolean)
    .join(' · ')

  return (
    <div data-testid={`task-${task.ext_id}`} data-status={task.status}
         className="rounded-lg border border-border bg-card p-3 transition-colors hover:border-primary/40">
      <div className="flex items-start gap-2">
        {isSuggested && <ConfidenceDot confidence={task.confidence} />}
        <p className="min-w-0 flex-1 text-[13px] font-semibold leading-snug text-foreground">
          {head}
        </p>
      </div>

      {/* Age is never conditional — an undecided card with no date on it cannot be
          triaged (Jonathan, 2026-08-12). */}
      <p className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[11px] leading-snug text-muted-foreground">
        {showProject && <ProjectPill task={task} workspace={workspace ?? routeWorkspace} />}
        <TaskAge createdAt={task.created_at} closedAt={task.ask_closed_at} />
        {meta && <span>· {meta}</span>}
      </p>

      {/* Whose court: the single ball signal — Echo working, or waiting on a human. */}
      {task.status === 'in_progress' && !askOpen && (
        <div className="mt-1.5">
          {echo ? <EchoWorking name={agentDisplayName(task.agent_slug)} /> : <WaitingChip who={task.assigned.trim()} />}
        </div>
      )}

      {/* THE ASK, on the card: what the agent needs from a person. */}
      {askOpen && (
        <div className="mt-2 rounded-md border border-warning/30 bg-warning/5 p-2" data-testid={`ask-${task.ext_id}`}>
          <span className="inline-flex items-center gap-1 rounded border border-warning/30 bg-warning/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-warning">
            asks you · {askKind}
          </span>
          {task.ask_body && (
            <Markdown className="mt-1.5 text-[12px] leading-snug text-foreground-secondary">
              {task.ask_body}
            </Markdown>
          )}
        </div>
      )}

      {/* Secondary line: the outcome, only when it differs from the headline. */}
      {showOutcome && (
        <p className="mt-1.5 text-[11px] leading-snug text-muted-foreground">{outcome}</p>
      )}

      {/* Context: why this is here (esp. for validating suggested tasks). The
          grounded source links inline with the rationale when both are present. */}
      <RationaleLine rationale={task.rationale} sourceUrl={task.source_url} />

      {/* What the agent last did on this task — the stored action outcome. */}
      {lastApplied && <LastActivityLine action={lastApplied} />}

      {(task.owner ||
        task.due ||
        (!hasRationale && task.source_url) ||
        (task.links && task.links.length > 0)) && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <OwnerTag owner={task.owner} />
          {task.due && <DueChip due={task.due} done={isDone} />}
          {/* Source rides the rationale line when there is one; otherwise show a chip. */}
          {!hasRationale && <SourceChip url={task.source_url} />}
          {task.links?.map((l, i) => (
            <TaskLinkChip key={`${l.url}-${i}`} label={l.label} url={l.url} />
          ))}
        </div>
      )}

      {task.notes && task.notes.trim() && (
        <p
          className="mt-2 truncate text-[10px] text-muted-foreground/80"
          title={task.notes}
        >
          {task.notes}
        </p>
      )}

      <TaskActions task={task} canEdit={canEdit} onChanged={onChanged} />
    </div>
  )
}

// ── Sections ────────────────────────────────────────────────────────────────

function SectionHeader({
  label,
  count,
  dotClass,
  accent,
}: {
  label: string
  count: number
  dotClass?: string
  accent?: boolean
}): JSX.Element {
  return (
    <div
      className={`mb-2 flex items-center gap-2 border-b pb-1.5 ${
        accent ? 'border-warning/30' : 'border-border'
      }`}
    >
      {dotClass && <span className={`h-1.5 w-1.5 rounded-full ${dotClass}`} />}
      <span
        className={`text-[11px] font-bold uppercase tracking-[0.06em] ${
          accent ? 'text-warning' : 'text-foreground'
        }`}
      >
        {label}
      </span>
      <span className="ml-auto text-[11px] text-muted-foreground">{count}</span>
    </div>
  )
}

function CardGrid({
  tasks,
  onChanged,
  canEdit,
  lastByTask,
}: {
  tasks: TaskOut[]
  onChanged?: () => void
  canEdit: boolean
  lastByTask?: Map<string, TaskActionOut>
}): JSX.Element {
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      {tasks.map((t) => (
        <TaskCard
          key={t.ext_id}
          task={t}
          onChanged={onChanged}
          canEdit={canEdit}
          lastApplied={lastByTask?.get(t.ext_id)}
        />
      ))}
    </div>
  )
}

// One pending action, shown when the "N queued" badge is expanded: what's
// actually waiting (action · task · who · when) rather than just a count.
function PendingActionRow({ action }: { action: TaskActionOut }): JSX.Element {
  const who = (action.by || '').split('@')[0] || 'someone'
  return (
    <li className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5 text-[11px] text-muted-foreground">
      <span className="font-medium text-foreground">{actionVerb(action.action)}</span>
      {action.task_ext_id && <span className="truncate text-muted-foreground/90">{action.task_ext_id}</span>}
      <span className="text-muted-foreground/60">· {who}</span>
      <span className="text-muted-foreground/60">· {formatWhen(action.created_at)}</span>
    </li>
  )
}

// "N queued for <agent>" — actions the agent will drain on its next turn. Click
// to reveal *which* actions are pending, not just the number.
function QueuedForEcho({ pending }: { pending: TaskActionOut[] }): JSX.Element | null {
  const [open, setOpen] = useState(false)
  if (pending.length === 0) return null
  const name = agentDisplayName(pending[0].agent_slug)
  return (
    <div className="flex flex-col items-end gap-1.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 rounded border border-primary/30 bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary transition-colors hover:border-primary/50 hover:bg-primary/15"
        title="Actions queued for the agent to act on next turn"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-primary" />
        {pending.length} queued for {name}
        <span aria-hidden className="text-primary/70">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <ul className="w-full max-w-sm space-y-1 rounded-md border border-border bg-card p-2.5">
          {pending.map((a) => (
            <PendingActionRow key={a.id} action={a} />
          ))}
        </ul>
      )}
    </div>
  )
}

// A compact, collapsible activity stream of recent actions across the agent —
// reuses the actions the board already fetched. Newest first (API order).
function AgentActivity({ actions }: { actions: TaskActionOut[] }): JSX.Element | null {
  const [open, setOpen] = useState(false)
  if (actions.length === 0) return null
  const recent = actions.slice(0, 12)
  return (
    <section className="border-t border-border/60 pt-4">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-[0.06em] text-muted-foreground transition-colors hover:text-foreground"
      >
        <span aria-hidden className="text-muted-foreground/70">{open ? '▾' : '▸'}</span>
        Activity
        <span className="font-normal lowercase tracking-normal text-muted-foreground/70">
          {actions.length}
        </span>
      </button>
      {open && (
        <ul className="mt-2.5 space-y-1.5" data-testid="agent-activity">
          {recent.map((a) => {
            const who = (a.by || '').split('@')[0] || 'someone'
            const when = a.applied_at || a.created_at
            return (
              <li key={a.id} className="flex flex-wrap items-baseline gap-x-1.5 text-[11px] leading-snug">
                <span className="text-muted-foreground/60">{formatWhen(when)}</span>
                <span className="font-medium text-foreground">{who}</span>
                <span className="text-muted-foreground">{actionVerb(a.action)}</span>
                {a.task_ext_id && <span className="truncate text-muted-foreground/90">{a.task_ext_id}</span>}
                {a.status === 'pending' && (
                  <span className="rounded bg-primary/10 px-1 text-[10px] font-medium text-primary">queued</span>
                )}
                {a.result_note && (
                  <span className="basis-full truncate pl-1 text-muted-foreground/80" title={a.result_note}>
                    → {a.result_note}
                  </span>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

function byPosition(a: TaskOut, b: TaskOut): number {
  return a.position - b.position
}

export function TasksBoard({
  tasks,
  actions = [],
  onChanged,
  canEdit,
}: {
  tasks: TaskOut[]
  actions?: TaskActionOut[]
  onChanged?: () => void
  canEdit: boolean
}): JSX.Element {
  const suggested = tasks.filter((t) => t.status === 'suggested').sort(byPosition)
  const inProgress = tasks.filter((t) => t.status === 'in_progress')
  const waitingHuman = inProgress.filter((t) => !isAgent(t)).sort(byPosition)
  const echoWorking = inProgress.filter((t) => isAgent(t)).sort(byPosition)
  const done = tasks.filter((t) => t.status === 'done').sort(byPosition)
  const declined = tasks.filter((t) => t.status === 'declined').sort(byPosition)

  const pending = useMemo(() => actions.filter((a) => a.status === 'pending'), [actions])
  // Latest applied action per task — `actions` arrives newest-first, so the
  // first applied one we see for a task is its most recent outcome.
  const lastByTask = useMemo(() => {
    const m = new Map<string, TaskActionOut>()
    for (const a of actions) {
      if (a.status === 'applied' && a.task_ext_id && !m.has(a.task_ext_id)) {
        m.set(a.task_ext_id, a)
      }
    }
    return m
  }, [actions])

  if (tasks.length === 0) {
    return (
      <p className="text-[13px] text-muted-foreground">No tasks yet — nothing on the board.</p>
    )
  }

  const grid = (list: TaskOut[]) => (
    <CardGrid tasks={list} onChanged={onChanged} canEdit={canEdit} lastByTask={lastByTask} />
  )

  return (
    <div className="space-y-7">
      {pending.length > 0 && (
        <div className="flex justify-end">
          <QueuedForEcho pending={pending} />
        </div>
      )}

      {suggested.length > 0 && (
        <section>
          <SectionHeader
            label="Suggested"
            count={suggested.length}
            dotClass="bg-muted-foreground"
          />
          {grid(suggested)}
        </section>
      )}

      {waitingHuman.length > 0 && (
        <section className="rounded-xl border border-warning/25 bg-warning/5 p-3">
          <SectionHeader label="Waiting on a human" count={waitingHuman.length} accent />
          {grid(waitingHuman)}
        </section>
      )}

      {echoWorking.length > 0 && (
        <section>
          <SectionHeader
            label={`${agentDisplayName(echoWorking[0].agent_slug)} working`}
            count={echoWorking.length}
            dotClass="bg-primary"
          />
          {grid(echoWorking)}
        </section>
      )}

      {done.length > 0 && (
        <section>
          <SectionHeader label="Done" count={done.length} dotClass="bg-primary/40" />
          {grid(done)}
        </section>
      )}

      {declined.length > 0 && (
        <section className="opacity-60">
          <SectionHeader label="Declined" count={declined.length} />
          <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
            {declined.map((t) => (
              <div
                key={t.ext_id}
                data-testid={`task-${t.ext_id}`}
                data-status={t.status}
                className="rounded-md border border-border bg-card/50 px-3 py-1.5 text-[11px] text-muted-foreground"
                title={t.notes || undefined}
              >
                <span className="line-through decoration-muted-foreground/40">
                  {headline(t)}
                </span>
                {t.owner && <span className="ml-1.5 text-muted-foreground/70">· {t.owner}</span>}
              </div>
            ))}
          </div>
        </section>
      )}

      <AgentActivity actions={actions} />
    </div>
  )
}
