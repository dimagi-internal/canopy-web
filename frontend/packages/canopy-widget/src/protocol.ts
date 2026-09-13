/**
 * The host ↔ frame message protocol.
 *
 * Shared by both ends so the two cannot disagree about a field name — the frame
 * imports this same module. Every message is tagged `source: "canopy-widget"`
 * because a page's `message` handler hears everything anyone posts to that
 * window, including other widgets, extensions and ad frames; without a tag the
 * host would try to interpret them.
 *
 * **Direction matters for security, not just clarity.** The host sends the
 * token; the frame never sends one. So a `host→frame` message arriving *at the
 * host* is either a bug or an attempt, and is dropped by kind rather than
 * merely ignored by origin.
 */

export const SOURCE = 'canopy-widget' as const

/** frame → host */
export type FrameMessage =
  /** "I exist" — the only message posted before origins are known, and the only
   *  one that carries nothing. See the shell in apps/tokens/views_embed.py. */
  | { source: typeof SOURCE; type: 'ready' }
  /** The frame's token was rejected or is near expiry; mint another. */
  | { source: typeof SOURCE; type: 'token-request'; id: string }
  /** Read the host page's current state. */
  | { source: typeof SOURCE; type: 'context-request'; id: string }
  /** Run one of the host's registered actions. */
  | {
      source: typeof SOURCE
      type: 'action-request'
      id: string
      name: string
      args?: Record<string, unknown>
    }
  /** The frame wants the panel closed (the user hit its close button). */
  | { source: typeof SOURCE; type: 'close' }
  /** Desired panel height in `inline` mode, where the host owns layout. */
  | { source: typeof SOURCE; type: 'resize'; height: number }

/** host → frame */
export type HostMessage =
  | {
      source: typeof SOURCE
      type: 'init'
      token: string
      agent?: string
      metadata?: Record<string, unknown>
      actions: string[]
    }
  | { source: typeof SOURCE; type: 'token'; id: string; token: string }
  | { source: typeof SOURCE; type: 'token-error'; id: string; message: string }
  | { source: typeof SOURCE; type: 'context'; id: string; context: Record<string, unknown> }
  | { source: typeof SOURCE; type: 'action-result'; id: string; result: unknown }
  | { source: typeof SOURCE; type: 'action-error'; id: string; message: string }
  /** The set of callable actions changed while the panel was open. */
  | { source: typeof SOURCE; type: 'actions'; actions: string[] }
  | { source: typeof SOURCE; type: 'visibility'; open: boolean }

export function isFrameMessage(data: unknown): data is FrameMessage {
  if (typeof data !== 'object' || data === null) return false
  const m = data as Record<string, unknown>
  if (m.source !== SOURCE || typeof m.type !== 'string') return false
  // Only kinds the FRAME may send. A host-bound message ('init', 'token', …)
  // arriving here is not something to interpret — the host is the only party
  // that issues those, so seeing one means something is impersonating it.
  return ['ready', 'token-request', 'context-request', 'action-request', 'close', 'resize'].includes(
    m.type,
  )
}

/** The origin half of a `baseUrl`, which is the only value we will postMessage
 *  to or accept messages from.
 *
 *  A relative base (`/canopy`, the same-origin deployment) resolves against the
 *  current page, which is correct: the frame really is same-origin then. */
export function originOf(baseUrl: string, pageOrigin: string): string {
  return new URL(baseUrl, pageOrigin).origin
}
