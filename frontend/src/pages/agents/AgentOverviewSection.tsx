import { useEffect, useState } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import {
  listAgentSyncs,
  listAgentTasks,
  type AgentSyncOut,
  type AgentTaskOut,
  type AgentTaskStatus,
} from '@/api/agents'
import { enqueueTurn } from '@/api/harness'
import { RunnerAssignments } from '@/components/agents/RunnerAssignments'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { CountStat, SyncCard } from '@/components/agents/cards'
import { WorkbenchSubHeader, WorkbenchSkeleton } from 'canopy-ui'

// Dispatch a prompt straight to THIS agent from its own page — the per-agent
// counterpart to /supervisor's cross-fleet composer, so "act on this agent" doesn't
// require bouncing to the supervisor. Enqueues a turn; the runner claims + runs it
// (it waits in the queue if the runner is paused/offline).
function QuickTurn({ slug }: { slug: string }) {
  const [prompt, setPrompt] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sent, setSent] = useState(false)

  const send = async () => {
    const p = prompt.trim()
    if (!p) return
    setBusy(true)
    setError(null)
    try {
      await enqueueTurn({ agentSlug: slug, prompt: p })
      setPrompt('')
      setSent(true)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to dispatch')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mb-6 rounded-lg border border-border bg-card p-3">
      <span className="text-[11px] font-bold uppercase tracking-[0.06em] text-primary">Take a turn</span>
      <div className="mt-2 flex gap-2">
        <input
          value={prompt}
          onChange={(e) => {
            setPrompt(e.target.value)
            setSent(false)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void send()
          }}
          placeholder={`Dispatch a prompt to ${slug}…`}
          data-testid="quickturn-input"
          className="min-w-0 flex-1 rounded-md border border-input bg-input px-3 py-2 text-[13px] text-foreground"
        />
        <button
          type="button"
          disabled={busy || prompt.trim() === ''}
          onClick={() => void send()}
          className="shrink-0 rounded-md bg-primary px-4 py-2 text-[13px] font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
        >
          {busy ? 'Sending…' : 'Send'}
        </button>
      </div>
      {error && <p className="mt-1 text-[11px] text-destructive">{error}</p>}
      {sent && !error && (
        <p className="mt-1 text-[11px] text-success">Dispatched — the runner will pick it up.</p>
      )}
    </div>
  )
}

const TASK_COLUMNS: { status: AgentTaskStatus; label: string; dot: string }[] = [
  { status: 'suggested', label: 'Suggested', dot: 'bg-muted-foreground' },
  { status: 'in_progress', label: 'In progress', dot: 'bg-primary' },
  { status: 'done', label: 'Done', dot: 'bg-primary/40' },
  { status: 'declined', label: 'Declined', dot: 'bg-muted-foreground/40' },
]

const QUICK_LINKS: { to: string; label: string }[] = [
  { to: '../tasks', label: 'Task board' },
  { to: '../syncs', label: 'Syncs' },
  { to: '../work-products', label: 'Work products' },
  { to: '../skills', label: 'Skills' },
]

/**
 * A compact landing dashboard for an agent — NOT the full lists. Persona +
 * description, a counts row, the single latest sync, a condensed task summary
 * (counts per board column), and quick links into the deeper sections.
 */
export function AgentOverviewSection() {
  const { agent } = useOutletContext<AgentOutletContext>()
  const [latestSync, setLatestSync] = useState<AgentSyncOut | null>(null)
  const [tasks, setTasks] = useState<AgentTaskOut[] | null>(null)

  useEffect(() => {
    let cancelled = false
    setLatestSync(null)
    setTasks(null)
    void Promise.all([
      listAgentSyncs(agent.slug, { limit: 1 }),
      listAgentTasks(agent.slug),
    ])
      .then(([syncPage, taskList]) => {
        if (cancelled) return
        setLatestSync(syncPage.items[0] ?? null)
        setTasks(taskList)
      })
      .catch(() => {
        if (cancelled) return
        setLatestSync(null)
        setTasks([])
      })
    return () => {
      cancelled = true
    }
  }, [agent.slug])

  const tasksLoading = tasks === null
  const countFor = (status: AgentTaskStatus) =>
    (tasks ?? []).filter((t) => t.status === status).length

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader title="Overview" />

      {/* Persona / description */}
      {(agent.persona || agent.description) && (
        <div className="mb-6">
          {agent.persona && (
            <p className="text-[14px] text-foreground leading-relaxed">{agent.persona}</p>
          )}
          {agent.description && (
            <p className="text-[13px] text-muted-foreground leading-relaxed mt-2">{agent.description}</p>
          )}
        </div>
      )}

      {/* Dispatch a turn to this agent, inline */}
      <QuickTurn slug={agent.slug} />

      {/* Ranked runner assignments — which paired runners this agent routes to, in order */}
      <div className="mb-6 rounded-lg border border-border bg-card p-3">
        <span className="text-[11px] font-bold uppercase tracking-[0.06em] text-primary">
          Runner assignments
        </span>
        <p className="mt-1 mb-2 text-[11px] text-muted-foreground">
          Ranked runners for {agent.name}&apos;s turns. The top online + ready runner claims first.
        </p>
        <RunnerAssignments agentSlug={agent.slug} />
      </div>

      {/* Counts row */}
      <div className="flex flex-wrap gap-6 pb-6 mb-6 border-b border-border">
        <CountStat value={agent.task_count} label="Tasks" />
        <CountStat value={agent.sync_count} label="Syncs" />
        <CountStat value={agent.work_product_count} label="Work" />
        <CountStat value={agent.skill_count} label="Skills" />
      </div>

      {/* Task summary — counts per board column */}
      <div className="mb-8">
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="text-[11px] font-bold uppercase tracking-[0.08em] text-primary">
            Tasks
          </h2>
          <Link
            to="../tasks"
            className="text-[11px] text-muted-foreground hover:text-primary transition-colors"
          >
            Open board →
          </Link>
        </div>
        {tasksLoading ? (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {TASK_COLUMNS.map((c) => (
              <div key={c.status} className="h-16 rounded-lg bg-muted border border-border animate-pulse" />
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {TASK_COLUMNS.map((c) => (
              <div
                key={c.status}
                className="rounded-lg bg-card border border-border px-3 py-3"
              >
                <div className="flex items-center gap-2">
                  <span className={`h-1.5 w-1.5 rounded-full ${c.dot}`} />
                  <span className="text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
                    {c.label}
                  </span>
                </div>
                <span className="block text-lg font-semibold text-foreground leading-none mt-2">
                  {countFor(c.status)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Latest sync */}
      <div className="mb-8">
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="text-[11px] font-bold uppercase tracking-[0.08em] text-primary">
            Latest sync
          </h2>
          <Link
            to="../syncs"
            className="text-[11px] text-muted-foreground hover:text-primary transition-colors"
          >
            All syncs →
          </Link>
        </div>
        {latestSync === null && tasksLoading ? (
          <WorkbenchSkeleton rows={1} />
        ) : latestSync ? (
          <SyncCard sync={latestSync} />
        ) : (
          <p className="text-[13px] text-muted-foreground">No syncs yet.</p>
        )}
      </div>

      {/* Quick links */}
      <div className="flex flex-wrap gap-2">
        {QUICK_LINKS.map((l) => (
          <Link
            key={l.to}
            to={l.to}
            className="inline-flex items-center gap-1 text-[12px] font-medium text-muted-foreground hover:text-primary bg-card border border-border hover:border-primary/40 px-3 py-1.5 rounded-md transition-colors"
          >
            {l.label}
            <span className="text-primary/70">→</span>
          </Link>
        ))}
      </div>
    </div>
  )
}
