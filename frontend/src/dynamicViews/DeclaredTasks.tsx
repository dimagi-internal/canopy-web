import type { DeclaredTask } from './agentTasks'

/**
 * What a view can make agents do — its declared agent tasks — said where a reader
 * of the view sees it. A view with none says nothing: it only reads.
 */
export function DeclaredTasks({ tasks, className = '' }: { tasks: readonly DeclaredTask[] | undefined; className?: string }) {
  if (!tasks?.length) return null
  return (
    <div className={`text-xs text-muted-foreground ${className}`}>
      Can give agents tasks, as you:{' '}
      {tasks.map((t, i) => (
        <span key={`${t.agent}/${t.name}`}>
          {i > 0 && ', '}
          <code className="text-foreground">{t.agent}</code> <code>{t.name}</code>
        </span>
      ))}
    </div>
  )
}
