import { apiV2, WORKSPACE_HEADER } from './client.v2'
import type { components } from './generated'
import type { WorkspaceOut } from './workspaces'

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

/**
 * The workspaces to pin so that, between them, every workspace you are in is
 * read once: a pinned read covers the workspaces below the pin, so pin the top
 * of each tree you can see (and any workspace whose parent's pin would not
 * reach it). The server never joins two organisations in one read; this is
 * how a page that spans them puts them side by side.
 */
export function attentionPins(workspaces: readonly WorkspaceOut[]): string[] {
  const bySlug = new Map(workspaces.map((w) => [w.slug, w]))
  return workspaces
    .filter((w) => {
      const parent = w.parent ? bySlug.get(w.parent) : undefined
      return !parent || !parent.pinned_read_covers_descendants
    })
    .map((w) => w.slug)
}

function unwrap<T>(res: { data?: T; error?: unknown; response?: { status: number } }, what: string): T {
  if (res.error !== undefined || res.data === undefined) {
    throw new Error(`${what} failed (${res.response?.status ?? '?'})`)
  }
  return res.data
}

async function readOne(workspace: string): Promise<Attention> {
  const res = await apiV2.GET('/api/attention/', { headers: { [WORKSPACE_HEADER]: workspace } })
  // openapi-fetch's Readable<T> degrades readonly arrays to ArrayLike; read the plain shape.
  const body = unwrap(res, 'listAttention') as unknown as Record<'now' | 'quiet' | 'hidden' | 'running', AttentionRowWire[]>
  return {
    now: Array.from(body.now, normalize),
    quiet: Array.from(body.quiet, normalize),
    hidden: Array.from(body.hidden, normalize),
    running: Array.from(body.running ?? [], normalize),
  }
}

const newestFirst = (a: AttentionRow, b: AttentionRow) =>
  Date.parse(b.last_activity_at) - Date.parse(a.last_activity_at)

/** Everything waiting on you across every workspace you are in, newest first. */
export async function loadAttention(workspaces: readonly WorkspaceOut[]): Promise<Attention> {
  const parts = await Promise.all(attentionPins(workspaces).map(readOne))
  const merged: Attention = { now: [], quiet: [], hidden: [], running: [] }
  const seen = new Set<string>()
  for (const group of ['now', 'quiet', 'hidden', 'running'] as const) {
    for (const part of parts) {
      for (const item of part[group]) {
        if (seen.has(item.key)) continue
        seen.add(item.key)
        merged[group].push(item)
      }
    }
    merged[group].sort(newestFirst)
  }
  return merged
}

/** Take things off your own list and nothing more: no task is closed or
 *  decided and nobody is told. Each returns when something new happens on it,
 *  or after `days`. */
export async function hide(keys: string[], days?: number): Promise<number> {
  const res = await apiV2.POST('/api/attention/hide', { body: { keys, days: days ?? null } })
  return unwrap(res, 'hide').count
}

/** Put something you hid back on your list. */
export async function unhide(key: string): Promise<void> {
  const res = await apiV2.DELETE('/api/attention/hide/{key}', { params: { path: { key } } })
  unwrap(res, 'unhide')
}
