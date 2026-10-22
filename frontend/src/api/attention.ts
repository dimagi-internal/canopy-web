import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

type AttentionRowWire = components['schemas']['AttentionRowOut']
/** The wire item with its defaults filled in (openapi-fetch degrades readonly
 *  arrays to ArrayLike, so they are rebuilt here once). */
export type AttentionRow = Omit<AttentionRowWire, 'actions' | 'target'> & {
  actions: string[]
  target: Record<string, string>
}

const normalize = (i: AttentionRowWire): AttentionRow => ({
  ...i,
  actions: Array.from(i.actions ?? []),
  target: { ...(i.target ?? {}) },
})
export type Attention = { now: AttentionRow[]; quiet: AttentionRow[]; hidden: AttentionRow[]; running: AttentionRow[] }
/** What an item asks of you — branch on this, never on `reason` (display text). */
export type AttentionState = AttentionRow['state']


/** Everything waiting on you across every workspace you are in, newest first:
 *  one user-level read (`/api/me/attention/`) — the server reads each
 *  organisation on its own and labels every row with it. */
export async function loadAttention(): Promise<Attention> {
  const res = await apiV2.GET('/api/me/attention/')
  // openapi-fetch's Readable<T> degrades readonly arrays to ArrayLike; read the plain shape.
  const body = unwrap(res, 'listAttention') as unknown as Record<keyof Attention, AttentionRowWire[]>
  return {
    now: Array.from(body.now, normalize),
    quiet: Array.from(body.quiet, normalize),
    hidden: Array.from(body.hidden, normalize),
    running: Array.from(body.running ?? [], normalize),
  }
}

/** Take things off your own list and nothing more: no task is closed or
 *  decided and nobody is told. Each returns when something new happens on it,
 *  or after `days`. */
export async function hide(keys: string[], days?: number): Promise<number> {
  const res = await apiV2.POST('/api/me/attention/hide', { body: { keys, days: days ?? null } })
  return unwrap(res, 'hide').count
}

/** Put something you hid back on your list. */
export async function unhide(key: string): Promise<void> {
  const res = await apiV2.DELETE('/api/me/attention/hide/{key}', { params: { path: { key } } })
  unwrap(res, 'unhide')
}
