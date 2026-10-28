import type { DeclaredTask } from './agentTasks'

/**
 * What a view can make agents do — its declared agent tasks — said where a reader
 * of the view sees it. A view with none says nothing: it only reads.
 */
export function DeclaredTasks({ tasks, className = '' }: { tasks: readonly DeclaredTask[] | undefined; className?: string }) {
  if (!tasks?.length) return null
  const agents = [...new Set(tasks.map((t) => t.agent))]
  const names = agents.map((a) => a.charAt(0).toUpperCase() + a.slice(1))
  return (
    <details className={`text-xs text-muted-foreground ${className}`}>
      <summary className="cursor-pointer">
        This page can ask {names.join(' and ')} to do work for you, as you
      </summary>
      <div className="mt-1">
        {tasks.map((t, i) => (
          <span key={`${t.agent}/${t.name}`}>
            {i > 0 && ', '}
            <code className="text-foreground">{t.agent}</code> <code>{t.name}</code>
          </span>
        ))}
      </div>
    </details>
  )
}
