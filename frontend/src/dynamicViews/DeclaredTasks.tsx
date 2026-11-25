import type { DeclaredTask } from './agentTasks'

const MOVES: Record<string, string> = { done: 'mark them done', reopen: 'reopen them' }

/**
 * What a view can make happen — its declared agent tasks, and the status moves it may make on its
 * project's tasks (`task_actions`) — said where a reader of the view sees it. A view with neither
 * says nothing: it only reads.
 */
export function DeclaredTasks({ tasks, taskActions, className = '' }: {
  tasks: readonly DeclaredTask[] | undefined
  taskActions?: readonly string[]
  className?: string
}) {
  const moves = (taskActions ?? []).map((a) => MOVES[a] ?? a)
  return (
    <>
      {!!tasks?.length && <AgentTasks tasks={tasks} className={className} />}
      {moves.length > 0 && (
        <p className={`text-xs text-muted-foreground ${className}`}>
          A click on this page can change this project’s tasks for you, as you: {moves.join(' or ')}
        </p>
      )}
    </>
  )
}

function AgentTasks({ tasks, className }: { tasks: readonly DeclaredTask[]; className: string }) {
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
