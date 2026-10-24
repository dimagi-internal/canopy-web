// A live task list: fetched once, edited optimistically (a row changes the
// moment you pick, and goes back with the server's reason if it refuses), and
// re-read — debounced — whenever any task in the workspace moves (`task://`).
import { useCallback, useEffect, useRef, useState } from 'react'

import { actOn, updateTask, type Task, type TaskActionName, type TaskPatch } from '@/api/work'
import { TASK_RESOURCE, useResource } from '@/widget/useResource'

/** How long a burst of task changes waits before the list re-reads. */
const REFRESH_DEBOUNCE_MS = 600

export interface TaskList {
  tasks: Task[] | null
  error: string | null
  reload: () => void
  /** Change one task's fields; the row updates at once. Throws the server's refusal. */
  patch: (key: string, body: TaskPatch, optimistic?: Partial<Task>) => Promise<void>
  /** Do an action to one task (approve, accept, done …). */
  act: (key: string, action: TaskActionName, comment?: string) => Promise<void>
  setTasks: (fn: (tasks: Task[]) => Task[]) => void
}

export function useTaskList(fetcher: () => Promise<Task[]>, deps: unknown[]): TaskList {
  const [tasks, setTasksState] = useState<Task[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const latest = useRef(fetcher)
  latest.current = fetcher
  const seq = useRef(0)

  const reload = useCallback(() => {
    const mine = ++seq.current
    latest
      .current()
      .then((rows) => {
        if (mine !== seq.current) return
        setTasksState(rows)
        setError(null)
      })
      .catch((e: unknown) => {
        if (mine !== seq.current) return
        setError(e instanceof Error ? e.message : 'Could not load tasks')
      })
  }, [])

  // eslint-disable-next-line react-hooks/exhaustive-deps -- the caller names what the fetch depends on
  useEffect(() => reload(), deps)

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useResource(TASK_RESOURCE, () => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(reload, REFRESH_DEBOUNCE_MS)
  })
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])

  const setTasks = useCallback((fn: (t: Task[]) => Task[]) => setTasksState((cur) => (cur ? fn(cur) : cur)), [])

  const patch = useCallback(
    async (key: string, body: TaskPatch, optimistic?: Partial<Task>) => {
      let before: Task | undefined
      setTasks((rows) =>
        rows.map((t) => {
          if (t.key !== key) return t
          before = t
          return { ...t, ...(optimistic ?? (body as Partial<Task>)) }
        }),
      )
      try {
        const saved = await updateTask(key, body)
        setTasks((rows) => rows.map((t) => (t.key === key ? saved : t)))
      } catch (e) {
        if (before) {
          const restore = before
          setTasks((rows) => rows.map((t) => (t.key === key ? restore : t)))
        }
        throw e
      }
    },
    [setTasks],
  )

  const act = useCallback(
    async (key: string, action: TaskActionName, comment = '') => {
      const res = await actOn(key, action, comment)
      setTasks((rows) => rows.map((t) => (t.key === key ? res.task : t)))
    },
    [setTasks],
  )

  return { tasks, error, reload, patch, act, setTasks }
}
