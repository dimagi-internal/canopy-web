// Small helpers every typed API module (agents, work, checkins) shares around
// openapi-fetch calls. Kept apart from `client.v2` so a test that mocks the client
// still gets the real helpers.
import { WORKSPACE_HEADER } from './client.v2'

/**
 * Request options pinning a call to one workspace (`{headers}`), or `{}` when none
 * is named — spread it into an openapi-fetch call: `apiV2.GET(path, { ...pin(ws) })`.
 */
export function pin(workspace?: string): { headers?: Record<string, string> } {
  return workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {}
}

/**
 * The generated shapes are readonly (--immutable), and openapi-fetch's Readable<T>
 * degrades their nested readonly arrays to ArrayLike, so a response is not
 * assignable to its own alias. The JSON is the alias's shape; bridge the type at
 * this one boundary.
 */
export function typed<T>(value: unknown): T {
  return value as T
}

/** A query without its blank parameters (`''`, `undefined`, `null`) — they would be sent as `?k=`. */
export function clean<T extends Record<string, unknown>>(q: T): T {
  return Object.fromEntries(Object.entries(q).filter(([, v]) => v !== '' && v !== undefined && v !== null)) as T
}
