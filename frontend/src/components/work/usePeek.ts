// The task peek: a task's page in a side panel beside the list it was opened
// from. Its state is the `?peek=<KEY>` query parameter, so the list's own URL
// (and the panel) survive a reload and a shared link; the task's one page is
// still `/w/<ws>/tasks/<KEY>`, which the panel links to.
import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

export function usePeek(): { peek: string | null; open: (key: string) => void; close: () => void } {
  const [params, setParams] = useSearchParams()
  const peek = params.get('peek')
  const open = useCallback(
    (key: string) =>
      setParams(
        (p) => {
          const next = new URLSearchParams(p)
          next.set('peek', key)
          return next
        },
        { replace: true },
      ),
    [setParams],
  )
  const close = useCallback(
    () =>
      setParams(
        (p) => {
          const next = new URLSearchParams(p)
          next.delete('peek')
          return next
        },
        { replace: true },
      ),
    [setParams],
  )
  return { peek, open, close }
}
