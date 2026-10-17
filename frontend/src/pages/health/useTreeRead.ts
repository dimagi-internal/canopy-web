import { useEffect, useState } from 'react'

import { WorkspaceApiError } from '@/api/workspaces'

/** One admin-tier read of a workspace tree (health, analytics): loading, the
 *  403/404 a non-admin gets, any other error, and the data. Re-runs when `key`
 *  changes; a stale response from an earlier key is dropped. */
export function useTreeRead<T>(load: () => Promise<T>, key: string): {
  data: T | null
  error: string
  forbidden: boolean
} {
  const [state, setState] = useState<{ key: string; data: T | null; error: string; forbidden: boolean }>(
    { key: '', data: null, error: '', forbidden: false })

  useEffect(() => {
    let off = false
    load()
      .then((data) => { if (!off) setState({ key, data, error: '', forbidden: false }) })
      .catch((e: unknown) => {
        if (off) return
        const forbidden = e instanceof WorkspaceApiError && (e.status === 403 || e.status === 404)
        setState({ key, data: null, forbidden, error: forbidden ? '' : e instanceof Error ? e.message : String(e) })
      })
    return () => { off = true }
    // `load` is rebuilt every render; `key` names what it loads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  // A response for an earlier key is not this key's answer: read as loading.
  return state.key === key ? state : { data: null, error: '', forbidden: false }
}
