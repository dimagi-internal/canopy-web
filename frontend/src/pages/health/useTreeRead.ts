import { ApiError } from '@/api/problem'
import { useLoad } from '@/hooks/useLoad'

/** One admin-tier read of a workspace tree (health, analytics): loading, the
 *  403/404 a non-admin gets, any other error, and the data. Re-runs when `key`
 *  changes; a stale response from an earlier key is dropped. */
export function useTreeRead<T>(load: () => Promise<T>, key: string): {
  data: T | null
  error: string
  forbidden: boolean
} {
  // `load` is rebuilt every render; `key` names what it loads.
  const { data, error } = useLoad(() => load(), [key])
  const forbidden = error instanceof ApiError && (error.status === 403 || error.status === 404)
  return { data, forbidden, error: error && !forbidden ? error.message : '' }
}
