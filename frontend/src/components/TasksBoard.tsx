import { useMemo, useState, type JSX, type ReactNode } from 'react'
import {
  postTaskCommand,
  type AgentCommandKind,
  type AgentCommandOut,
  type AgentTaskOut,
} from '@/api/agents'
import { decideItem, type ItemDecision } from '@/api/items'

// ── "Who has the ball" model ───────────────────────────────────────────────
// The board is organized by whose court the next action sits in, not by equal
// status columns. `assigned` names who the next step waits on: the agent
// ("Echo") or a human. Empty or case-insensitive 'echo' means the agent.

function isEcho(assigned: string): boolean {
  const a = (assigned || '').trim().toLowerCase()
  return a === '' || a === 'echo'
}

function headline(task: AgentTaskOut): string {
  return (task.next_action || '').trim() || (task.title || '').trim()
}

function formatDue(s: string): string {
  return new Date(s).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
  })
}

// A short, human "when" for command timestamps: "Jun 17, 2:30 PM".
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

// A command's kind read as a past-tense verb for activity rows.
const KIND_VERB: Record<string, string> = {
  accept: 'accepted',
  decline: 'declined',
  dispatch: 'dispatched',
  reassign: 'reassigned',
  edit: 'edited',
  comment: 'commented on',
  done: 'completed',
}

function kindVerb(kind: string): string {
  return KIND_VERB[kind] ?? kind
}

function isPastDue(due: string): boolean {
  const d = new Date(due)
  if (Number.isNaN(d.getTime())) return false
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return d.getTime() < today.getTime()
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

// The "ball is in the agent's court" affordance: Echo + a pulsing dot.
function EchoWorking(): JSX.Element {
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-primary">
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary/60" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-primary" />
      </span>
      Echo
    </span>
  )
}

// The "ball is in a human's court" affordance: an amber waiting chip.
// THE ASK, on the card.
//
// An item stopped being its own model on 2026-09-20 (#871/#873): the ask moved
// onto the task, the rows were migrated, and `/items/` became a view of tasks.
// The board was never told — it read none of `ask_kind`/`ask_state`, so a task
// blocking on a human decision rendered as ordinary work in flight and could
// only be decided on Inbox or Items. That is the exact failure the merge was
// meant to end, left in place because the UI half never shipped.
function AskOnCard({ task, onChanged }: { task: AgentTaskOut; onChanged?: () => void }): JSX.Element | null {
  const [busy, setBusy] = useState<ItemDecision | null>(null)
  const [error, setError] = useState<string | null>(null)
  const kind = (task.ask_kind || '').trim()
  if (!kind || task.ask_state !== 'open') return null

  const decide = (decision: ItemDecision) => {
    setBusy(decision)
    setError(null)
    decideItem(task.uuid, decision)
      .then(() => onChanged?.())
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'could not record that'))
      .finally(() => setBusy(null))
  }

  return (
    <div className="mt-2 rounded-md border border-warning/30 bg-warning/5 p-2" data-testid={`ask-${task.ext_id}`}>
      <span className="inline-flex items-center gap-1 rounded border border-warning/30 bg-warning/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-warning">
        asks you · {kind}
      </span>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {(['implement', 'skip', 'defer'] as const).map((d) => (
          <button
            key={d}
            type="button"
            disabled={busy !== null}
            onClick={() => decide(d)}
            data-testid={`ask-${d}-${task.ext_id}`}
            className="min-h-8 rounded-md border border-border px-2 py-0.5 text-[11px] text-foreground hover:bg-muted disabled:opacity-40"
          >
            {busy === d ? '…' : d}
          </button>
        ))}
      </div>
      {error && <p className="mt-1 text-[11px] text-destructive">{error}</p>}
    </div>
  )
}

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
// a suggested task before accepting it. When `sourceUrl` is set, the grounded
// source Echo read is linked inline right after the rationale — the trust signal.
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

// The outcome of the most recent command Echo applied to this task — surfaces
// `result_note` + `applied_at` that the API already stores but nothing rendered.
function LastActivityLine({ command }: { command: AgentCommandOut }): JSX.Element {
  const note = (command.result_note || '').trim() || `${kindVerb(command.kind)}`
  const when = command.applied_at ? formatWhen(command.applied_at) : ''
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

// ── Actions ──────────────────────────────────────────────────────────────────
// Secondary, muted affordances that POST a command to the queue. Deliberately
// quieter than the next-action headline.

// A small text button used for muted in-card actions.
function ActionButton({
  children,
  onClick,
  disabled,
  tone = 'muted',
}: {
  children: ReactNode
  onClick: () => void
  disabled?: boolean
  tone?: 'muted' | 'primary' | 'destructive'
}): JSX.Element {
  const toneClass =
    tone === 'primary'
      ? 'text-primary hover:text-primary/80'
      : tone === 'destructive'
        ? 'text-destructive/90 hover:text-destructive'
        : 'text-muted-foreground hover:text-foreground'
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`text-[11px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${toneClass}`}
    >
      {children}
    </button>
  )
}

// Hosts a card's command-posting state: in-flight lock, error surfacing, and a
// decline reason input. Renders the actions appropriate to the task's status.
function TaskActions({
  task,
  onChanged,
}: {
  task: AgentTaskOut
  onChanged?: () => void
}): JSX.Element | null {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [declining, setDeclining] = useState(false)
  const [reason, setReason] = useState('')

  async function run(kind: AgentCommandKind, payload?: Record<string, unknown>) {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await postTaskCommand(task.agent_slug, task.id, kind, payload)
      onChanged?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Action failed')
      setBusy(false)
    }
    // On success we leave `busy` true: the board is about to refetch and this
    // card will be replaced, so re-enabling would only flash.
  }

  const isSuggested = task.status === 'suggested'
  const isInProgress = task.status === 'in_progress'
  if (!isSuggested && !isInProgress) return null

  return (
    <div className="mt-2.5 border-t border-border/60 pt-2">
      {isSuggested && !declining && (
        <div className="flex items-center gap-3">
          <ActionButton tone="primary" disabled={busy} onClick={() => run('accept')}>
            Accept
          </ActionButton>
          {/* One-click decline: no reason required (the reason only ever feeds the
              agent as context). Use "＋ reason" when you actually want to steer it. */}
          <ActionButton tone="muted" disabled={busy} onClick={() => run('decline', { reason: '' })}>
            Decline
          </ActionButton>
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              setError(null)
              setDeclining(true)
            }}
            className="text-[11px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline disabled:opacity-50"
            title="Decline with a reason for the agent"
          >
            ＋ reason
          </button>
        </div>
      )}

      {isSuggested && declining && (
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="text"
            autoFocus
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Reason (optional)"
            disabled={busy}
            className="h-7 min-w-0 flex-1 rounded border border-border bg-muted px-2 text-[11px] text-foreground outline-none placeholder:text-muted-foreground/60 focus:border-primary/50"
            onKeyDown={(e) => {
              if (e.key === 'Enter') run('decline', { reason: reason.trim() })
              if (e.key === 'Escape') setDeclining(false)
            }}
          />
          <ActionButton
            tone="destructive"
            disabled={busy}
            onClick={() => run('decline', { reason: reason.trim() })}
          >
            Confirm decline
          </ActionButton>
          <ActionButton tone="muted" disabled={busy} onClick={() => setDeclining(false)}>
            Cancel
          </ActionButton>
        </div>
      )}

      {isInProgress && (
        <div className="flex items-center gap-3">
          <ActionButton tone="primary" disabled={busy} onClick={() => run('dispatch')}>
            Echo, do this now
          </ActionButton>
          <ActionButton tone="muted" disabled={busy} onClick={() => run('done')}>
            Mark done
          </ActionButton>
        </div>
      )}

      {error && <p className="mt-1.5 text-[10px] text-destructive">{error}</p>}
    </div>
  )
}

// ── Card ────────────────────────────────────────────────────────────────────

export function TaskCard({
  task,
  onChanged,
  lastApplied,
}: {
  task: AgentTaskOut
  onChanged?: () => void
  lastApplied?: AgentCommandOut
}): JSX.Element {
  const head = headline(task)
  const outcome = (task.title || '').trim()
  const showOutcome = outcome && outcome !== head
  const echo = isEcho(task.assigned)
  const isSuggested = task.status === 'suggested'
  const isDone = task.status === 'done'
  const hasRationale = Boolean((task.rationale || '').trim())

  return (
    <div data-testid={`task-${task.ext_id}`} data-status={task.status}
         className="rounded-lg border border-border bg-card p-3 transition-colors hover:border-primary/40">
      <div className="flex items-start gap-2">
        {isSuggested && <ConfidenceDot confidence={task.confidence} />}
        <p className="min-w-0 flex-1 text-[13px] font-semibold leading-snug text-foreground">
          {head}
        </p>
      </div>

      {/* Whose court: the single ball signal — Echo working, or waiting on a human. */}
      {!isSuggested && !isDone && task.status === 'in_progress' && (
        <div className="mt-1.5">
          {echo ? <EchoWorking /> : <WaitingChip who={task.assigned.trim()} />}
        </div>
      )}

      <AskOnCard task={task} onChanged={onChanged} />

      {/* Secondary line: the outcome, only when it differs from the headline. */}
      {showOutcome && (
        <p className="mt-1.5 text-[11px] leading-snug text-muted-foreground">{outcome}</p>
      )}

      {/* Context: why this is here (esp. for validating suggested tasks). The
          grounded source links inline with the rationale when both are present. */}
      <RationaleLine rationale={task.rationale} sourceUrl={task.source_url} />

      {/* What Echo last did on this task — the stored command outcome. */}
      {lastApplied && <LastActivityLine command={lastApplied} />}

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

      <TaskActions task={task} onChanged={onChanged} />
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
  lastByTask,
}: {
  tasks: AgentTaskOut[]
  onChanged?: () => void
  lastByTask?: Map<number, AgentCommandOut>
}): JSX.Element {
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      {tasks.map((t) => (
        <TaskCard
          key={t.id}
          task={t}
          onChanged={onChanged}
          lastApplied={lastByTask?.get(t.id)}
        />
      ))}
    </div>
  )
}

// One pending command, shown when the "N queued for Echo" badge is expanded:
// what's actually waiting (kind · task · who · when) rather than just a count.
function PendingCommandRow({ command }: { command: AgentCommandOut }): JSX.Element {
  const who = (command.created_by || '').split('@')[0] || 'someone'
  return (
    <li className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5 text-[11px] text-muted-foreground">
      <span className="font-medium text-foreground">{kindVerb(command.kind)}</span>
      {command.task_title && <span className="truncate text-muted-foreground/90">{command.task_title}</span>}
      <span className="text-muted-foreground/60">· {who}</span>
      <span className="text-muted-foreground/60">· {formatWhen(command.created_at)}</span>
    </li>
  )
}

// "N queued for Echo" — accept/dispatch commands Echo will drain on its next
// turn. Click to reveal *which* commands are pending, not just the number.
function QueuedForEcho({ pending }: { pending: AgentCommandOut[] }): JSX.Element | null {
  const [open, setOpen] = useState(false)
  if (pending.length === 0) return null
  return (
    <div className="flex flex-col items-end gap-1.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 rounded border border-primary/30 bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary transition-colors hover:border-primary/50 hover:bg-primary/15"
        title="Commands queued for Echo to act on next turn"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-primary" />
        {pending.length} queued for Echo
        <span aria-hidden className="text-primary/70">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <ul className="w-full max-w-sm space-y-1 rounded-md border border-border bg-card p-2.5">
          {pending.map((c) => (
            <PendingCommandRow key={c.id} command={c} />
          ))}
        </ul>
      )}
    </div>
  )
}

// A compact, collapsible activity stream of recent commands across the agent —
// reuses the commands the board already fetched. Newest first (API order).
function AgentActivity({ commands }: { commands: AgentCommandOut[] }): JSX.Element | null {
  const [open, setOpen] = useState(false)
  if (commands.length === 0) return null
  const recent = commands.slice(0, 12)
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
          {commands.length}
        </span>
      </button>
      {open && (
        <ul className="mt-2.5 space-y-1.5" data-testid="agent-activity">
          {recent.map((c) => {
            const who = (c.created_by || '').split('@')[0] || 'someone'
            const when = c.applied_at || c.created_at
            return (
              <li key={c.id} className="flex flex-wrap items-baseline gap-x-1.5 text-[11px] leading-snug">
                <span className="text-muted-foreground/60">{formatWhen(when)}</span>
                <span className="font-medium text-foreground">{who}</span>
                <span className="text-muted-foreground">{kindVerb(c.kind)}</span>
                {c.task_title && <span className="truncate text-muted-foreground/90">{c.task_title}</span>}
                {c.status === 'pending' && (
                  <span className="rounded bg-primary/10 px-1 text-[10px] font-medium text-primary">queued</span>
                )}
                {c.result_note && (
                  <span className="basis-full truncate pl-1 text-muted-foreground/80" title={c.result_note}>
                    → {c.result_note}
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

function byPosition(a: AgentTaskOut, b: AgentTaskOut): number {
  return a.position - b.position
}

export function TasksBoard({
  tasks,
  onChanged,
  commands = [],
}: {
  tasks: AgentTaskOut[]
  onChanged?: () => void
  commands?: AgentCommandOut[]
}): JSX.Element {
  const suggested = tasks.filter((t) => t.status === 'suggested').sort(byPosition)
  const inProgress = tasks.filter((t) => t.status === 'in_progress')
  const waitingHuman = inProgress.filter((t) => !isEcho(t.assigned)).sort(byPosition)
  const echoWorking = inProgress.filter((t) => isEcho(t.assigned)).sort(byPosition)
  const done = tasks.filter((t) => t.status === 'done').sort(byPosition)
  const declined = tasks.filter((t) => t.status === 'declined').sort(byPosition)

  const pending = useMemo(() => commands.filter((c) => c.status === 'pending'), [commands])
  // Latest applied command per task — `commands` arrives newest-first, so the
  // first applied one we see for a task is its most recent outcome.
  const lastByTask = useMemo(() => {
    const m = new Map<number, AgentCommandOut>()
    for (const c of commands) {
      if (c.status === 'applied' && c.task_id != null && !m.has(c.task_id)) {
        m.set(c.task_id, c)
      }
    }
    return m
  }, [commands])

  if (tasks.length === 0) {
    return (
      <p className="text-[13px] text-muted-foreground">No tasks yet — nothing on the board.</p>
    )
  }

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
          <CardGrid tasks={suggested} onChanged={onChanged} lastByTask={lastByTask} />
        </section>
      )}

      {waitingHuman.length > 0 && (
        <section className="rounded-xl border border-warning/25 bg-warning/5 p-3">
          <SectionHeader label="Waiting on a human" count={waitingHuman.length} accent />
          <CardGrid tasks={waitingHuman} onChanged={onChanged} lastByTask={lastByTask} />
        </section>
      )}

      {echoWorking.length > 0 && (
        <section>
          <SectionHeader
            label="Echo working"
            count={echoWorking.length}
            dotClass="bg-primary"
          />
          <CardGrid tasks={echoWorking} onChanged={onChanged} lastByTask={lastByTask} />
        </section>
      )}

      {done.length > 0 && (
        <section>
          <SectionHeader label="Done" count={done.length} dotClass="bg-primary/40" />
          <CardGrid tasks={done} onChanged={onChanged} lastByTask={lastByTask} />
        </section>
      )}

      {declined.length > 0 && (
        <section className="opacity-60">
          <SectionHeader label="Declined" count={declined.length} />
          <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
            {declined.map((t) => (
              <div
                key={t.id}
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

      <AgentActivity commands={commands} />
    </div>
  )
}
