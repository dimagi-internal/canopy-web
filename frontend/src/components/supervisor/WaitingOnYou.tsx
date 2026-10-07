import type { JSX } from 'react'
import { listFleetTasks, type TaskOut } from '@/api/agents'
import { TaskCard } from '@/components/TasksBoard'

// The fleet's "Waiting on you" — every task across the agents you can see with an
// open ask, or parked on you. The same filter as an agent's Tasks page
// (`?waiting=me`) and the same card, fleet-wide (`GET /api/tasks/?waiting=me`).
// On a phone the ranked queue matters more than per-agent grouping, so each card
// is tagged with its agent instead. Fully actionable in place — no linking out.

// eslint-disable-next-line react-refresh/only-export-components -- the queue's one fetch, beside its only renderer
export function loadWaitingOnYou(): Promise<TaskOut[]> {
  return listFleetTasks({ waiting: 'me' })
}

// Reviews first (a yes/no), then questions (need typing), then tasks merely
// parked on you. Stable within a rank, so the server's order holds.
function rank(t: TaskOut): number {
  if (!t.ask_open) return 2
  return t.ask_kind === 'review' ? 0 : 1
}

export function WaitingOnYou({
  tasks,
  canEdit,
  onChanged,
}: {
  tasks: TaskOut[]
  /** Whether the viewer may dispatch / mark done on this task's agent. */
  canEdit: (task: TaskOut) => boolean
  onChanged: () => void
}): JSX.Element {
  if (tasks.length === 0) {
    return (
      <p
        className="rounded-lg border border-border bg-card p-3 text-[13px] text-muted-foreground"
        data-testid="waiting-empty"
      >
        Nothing waiting on you.
      </p>
    )
  }

  const ranked = [...tasks].sort((a, b) => rank(a) - rank(b))
  return (
    <div className="flex flex-col gap-2" data-testid="waiting-on-you">
      {ranked.map((task) => (
        <TaskCard
          key={`${task.agent_slug}/${task.ext_id}`}
          task={task}
          canEdit={canEdit(task)}
          onChanged={onChanged}
          showAgent
        />
      ))}
    </div>
  )
}
