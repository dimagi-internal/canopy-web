/**
 * The panel's act-as-you permission (a host grant): whether this tab asks its host
 * for one, what it holds now, and the person's "Stop".
 *
 * A host grant lets the agent read the page's site AS the person, within their own
 * access. The panel never asks for one on load: the first mint is plain, and the
 * panel asks only when the person acts — sends a message (the opening one or any
 * later one), or opens a conversation whose live card needs to read the site as
 * them. From then on every mint of this tab asks again (the host re-grants about
 * every five minutes while the tab stays open), until the person presses Stop —
 * which revokes the page's permission at once and stops asking until they send
 * again. An agent's TURN only ever reads through the grant of the page that last
 * acted in its conversation (canopy's `host_grants.lookup`), so a turn never gets
 * a grant the person did not give by acting here.
 */
import type { CanopyClient } from 'canopy-client'

/** One live grant as canopy describes it (`HostGrantOut`). */
export interface PageGrantInfo {
  id: number
  site: string
  agent: string
  agent_name: string
  scope: string
  granted_at: string
  expires_at: string
}

export interface PageGrant {
  /** Whether this tab's mints ask the host for a grant. */
  wanted(): boolean
  /** The grant the panel's current token carries, as last read; null for none. */
  current(): PageGrantInfo | null
  subscribe(listener: () => void): () => void
  /** The person acted: ask for a grant with the next mint, and read it. Resolves
   *  true when this call is what started asking (the caller then tells canopy the
   *  page acted in the open conversation), false when the tab was already asking. */
  ensure(): Promise<boolean>
  /** The person pressed Stop: revoke the page's permission, stop asking. */
  stop(): Promise<void>
  /** Re-read the grant the current token carries. */
  refresh(): Promise<void>
  dispose(): void
}

/** How often a tab that holds a grant re-reads it, so a Stop pressed elsewhere (or
 *  the grant running out) shows here, and the panel stops asking. */
const POLL_MS = 60_000

export function createPageGrant(opts: {
  client: () => CanopyClient
  isContact: () => boolean
  pollMs?: number
}): PageGrant {
  let wanted = false
  let current: PageGrantInfo | null = null
  let timer: ReturnType<typeof setInterval> | null = null
  const listeners = new Set<() => void>()
  const path = () => (opts.isContact() ? '/api/contact/host-grant' : '/api/embed/host-grant')
  const notify = () => listeners.forEach((l) => l())

  function poll(on: boolean): void {
    if (timer) clearInterval(timer)
    timer = on ? setInterval(() => void api.refresh(), opts.pollMs ?? POLL_MS) : null
  }

  function stopAsking(): void {
    wanted = false
    current = null
    poll(false)
    // The token in hand carries the old grant: the next mint must be a plain one.
    opts.client().invalidateToken()
  }

  const api: PageGrant = {
    wanted: () => wanted,
    current: () => current,
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    async ensure() {
      if (wanted) return false
      wanted = true
      // The token in hand was minted without one; the next call mints with one.
      opts.client().invalidateToken()
      poll(true)
      await api.refresh()
      return true
    },
    async stop() {
      try {
        await opts.client().rest.json(path(), { method: 'DELETE' })
      } finally {
        stopAsking()
        notify()
      }
    },
    async refresh() {
      let next: PageGrantInfo | null = null
      try {
        const res = await opts.client().rest.json<{ grant: PageGrantInfo | null }>(path())
        next = res?.grant ?? null
      } catch {
        // Unreadable is not a reason to stop asking; the line just shows nothing.
        next = null
      }
      // It had one and now has none: stopped elsewhere, or ran out. Stop asking
      // until the person acts again.
      if (wanted && current && !next) stopAsking()
      else current = next
      notify()
    },
    dispose() {
      poll(false)
      listeners.clear()
    },
  }
  return api
}

/** "3:42 pm", in the person's own locale and zone. */
export function untilLabel(iso: string): string {
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return ''
  return when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/** Whether a View's refusal is "no live grant" (canopy's View gate 6). */
export function isNoGrant(error: unknown): boolean {
  const detail = String((error as { detail?: unknown })?.detail ?? (error as Error)?.message ?? '')
  return detail.startsWith('no_grant')
}
