import { useState, type JSX } from 'react'
import { listMyTasks, type TaskOut } from '@/api/agents'
import { updateTask, type TaskPatch } from '@/api/work'
import { TaskPeek } from '@/components/work/TaskPeek'
import { TaskTable } from '@/components/work/TaskTable'
import { useDirectory } from '@/components/work/useDirectory'
import { usePeek } from '@/components/work/usePeek'
import { useActiveWorkspace } from '@/workspace/WorkspaceProvider'
import { TASK_RESOURCE, useResource } from '@/widget/useResource'

// The fleet's "Waiting on you" — every task across the agents you can see with an
// open ask, or parked on you, in every workspace you are in
// (`GET /api/me/tasks/?waiting=me`, a user-level view; the same filter as the
// Tasks page's Waiting on me). Shown as THE task table every list uses, ranked
// reviews first; a title opens the task beside the list, where you decide it.
// Status, priority and assignee change in place on tasks of the workspace
// you are in (its people and agents are the assignee choices).

export function loadWaitingOnYou(): Promise<TaskOut[]> {
  return listMyTasks({ waiting: 'me' })
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
  /** Whether the viewer may change this task's fields (an editor of its workspace). */
  canEdit: (task: TaskOut) => boolean
  onChanged: () => void
}): JSX.Element {
  // Any task moving anywhere may add to or settle this queue.
  useResource(TASK_RESOURCE, onChanged)
  const active = useActiveWorkspace() ?? ''
  const directory = useDirectory(active)
  const { open } = usePeek()
  const [error, setError] = useState<string | null>(null)

  if (tasks.length === 0) {
    return (
      <>
        <p
          className="rounded-lg border border-border bg-card p-3 text-[13px] text-muted-foreground"
          data-testid="waiting-empty"
        >
          Nothing waiting on you.
        </p>
        <TaskPeek />
      </>
    )
  }

  const ranked = [...tasks].sort((a, b) => rank(a) - rank(b))
  const onPatch = (key: string, body: TaskPatch) => {
    setError(null)
    updateTask(key, body)
      .then(onChanged)
      .catch((e: unknown) => setError(`${key}: ${e instanceof Error ? e.message : 'the server refused'}`))
  }
  return (
    <div className="flex flex-col gap-2" data-testid="waiting-on-you">
      {error && (
        <p role="alert" className="text-[12.5px] text-destructive">
          {error}
        </p>
      )}
      <TaskTable
        tasks={ranked}
        directory={directory}
        onPatch={onPatch}
        onOpen={(t) => open(t.key)}
        columns={['project', 'assignee', 'updated']}
        canEdit={(t) => canEdit(t) && t.workspace === active}
      />
      <TaskPeek />
    </div>
  )
}
