// /w/:workspace/tasks/:key — a task's one page (the peek is this, beside a list).
import type { JSX } from 'react'
import { Link, useParams } from 'react-router-dom'

import { TaskDetailView } from '@/components/work/TaskDetailView'
import { scopedPath } from '@/lib/scopedLinks'

export function TaskPage(): JSX.Element {
  const { workspace = '', taskKey = '' } = useParams()
  return (
    <div className="mx-auto max-w-3xl">
      <nav className="mb-3 text-[12px] text-muted-foreground">
        <Link to={scopedPath(workspace, '/tasks')} className="hover:text-primary">
          ← Tasks
        </Link>
      </nav>
      <TaskDetailView key={taskKey} taskKey={taskKey.toUpperCase()} />
    </div>
  )
}
