// The peek: the task's page in a panel beside the list (`?peek=<KEY>`). The
// list stays usable behind it; Esc closes it; "Open" goes to the full page.
import { useEffect, type JSX } from 'react'

import { TaskDetailView } from './TaskDetailView'
import { usePeek } from './usePeek'

export function TaskPeek(): JSX.Element | null {
  const { peek, close } = usePeek()
  useEffect(() => {
    if (!peek) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !(e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement)) close()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [peek, close])
  if (!peek) return null
  return (
    <aside
      aria-label={`Task ${peek}`}
      data-testid="task-peek"
      className="fixed inset-y-0 right-0 z-40 w-full max-w-[44rem] overflow-y-auto border-l border-border bg-background p-5 shadow-xl sm:top-12"
    >
      <TaskDetailView key={peek} taskKey={peek} onClose={close} inPanel />
    </aside>
  )
}
