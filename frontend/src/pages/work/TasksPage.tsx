// /w/:workspace/tasks — every task in the workspace, people's and agents'
// alike: My tasks, Waiting on me, Created by me and All, as one dense list.
import type { JSX } from 'react'
import { useParams } from 'react-router-dom'

import { TasksView } from '@/components/work/TasksView'

export function TasksPage(): JSX.Element {
  const { workspace = '' } = useParams()
  return (
    <div className="mx-auto max-w-6xl">
      <h1 className="mb-3 text-[18px] font-semibold text-foreground">Tasks</h1>
      <TasksView key={workspace} workspace={workspace} tabs storageKey={`canopy.tasks.${workspace}`} />
    </div>
  )
}
