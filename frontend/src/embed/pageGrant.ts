/**
 * The panel's act-as-you permission (a host grant): whether this tab asks its host
 * for one, what it holds now, the person's passkey confirmation, and their "Stop".
 *
 * A host grant lets the agent read the page's site AS the person, within their own
 * access. The panel never asks for one on load: the first mint is plain, and the
 * panel asks only when the person acts — sends a message (the opening one or any
 * later one), or opens a conversation whose live card needs to read the site as
 * them. What the host grants then is unusable until the person confirms it with
 * their passkey, here in canopy's frame (`confirm`); it lasts 30 minutes from that
 * touch. The panel's token is renewed every few minutes while the tab is open, and
 * each renewal carries the same confirmation and end (canopy's
 * `host_grants._inherit`), so one touch is one prompt. Stop revokes the page's
 * permission at once and stops asking until they send again. An agent's TURN only
 * ever reads through the confirmed grant of the page that last acted in its
 * conversation (canopy's `host_grants.lookup`).
 *
 * Only a canopy user has a passkey, so a contact's panel never asks.
 */
import type { CanopyClient } from 'canopy-client'

import { credentialToJSON, toRequestOptions } from '../auth/webauthn'

/** One live grant as canopy describes it (`HostGrantOut`). */
export interface PageGrantInfo {
  id: number
  site: string
  agent: string
  agent_name: string
  scope: string
  granted_at: string
  expires_at: string
  /** Confirmed with the person's passkey. Until then no agent may use it. */
  confirmed: boolean
}

export interface PageGrant {
  /** Whether this tab's mints ask the host for a grant. */
  wanted(): boolean
  /** The grant the panel's current token carries, as last read — confirmed or
   *  waiting for the passkey; null for none. */
  current(): PageGrantInfo | null
  subscribe(listener: () => void): () => void
  /** The person acted: ask for a grant with the next mint, and read it. Resolves
   *  true when this call is what started asking (the caller then tells canopy the
   *  page acted in the open conversation), false when the tab was already asking. */
  ensure(): Promise<boolean>
  /** The person confirmed with their passkey: run the WebAuthn ceremony against
   *  canopy (its own frame's origin) and mark the waiting grant confirmed. Call
   *  from a click: Safari runs WebAuthn only inside a user gesture. */
  confirm(): Promise<void>
  /** Resolves once nothing waits for the passkey: the grant is confirmed, declined
   *  (`stop`) or gone. A send awaits this, so its turn has what the person decided. */
  settled(): Promise<void>
  /** The person pressed Stop (or declined the passkey): revoke the page's
   *  permission, stop asking. */
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
  const path = '/api/embed/host-grant'
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
      // A contact has no passkey, so canopy never redeems them a grant.
      if (wanted || opts.isContact()) return false
      wanted = true
      // The token in hand was minted without one; the next call mints with one.
      opts.client().invalidateToken()
      poll(true)
      await api.refresh()
      return true
    },
    settled() {
      return new Promise<void>((resolve) => {
        const done = () => !current || current.confirmed
        if (done()) {
          resolve()
          return
        }
        const off = api.subscribe(() => {
          if (done()) {
            off()
            resolve()
          }
        })
      })
    },
    async confirm() {
      const rest = opts.client().rest
      const begun = await rest.json<{ options: Record<string, unknown> }>(
        `${path}/confirm/begin`, { method: 'POST' })
      const credential = (await navigator.credentials.get({
        publicKey: toRequestOptions(begun.options),
      })) as PublicKeyCredential | null
      if (!credential) throw new Error('No passkey was used')
      const res = await rest.json<{ grant: PageGrantInfo | null }>(`${path}/confirm/finish`, {
        method: 'POST',
        body: JSON.stringify({ credential: credentialToJSON(credential) }),
      })
      current = res?.grant ?? null
      notify()
    },
    async stop() {
      try {
        await opts.client().rest.json(path, { method: 'DELETE' })
      } finally {
        stopAsking()
        notify()
      }
    },
    async refresh() {
      let next: PageGrantInfo | null = null
      try {
        const res = await opts.client().rest.json<{ grant: PageGrantInfo | null }>(path)
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
