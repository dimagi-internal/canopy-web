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

import type { SafeTheme } from './theme'

export const SOURCE = 'canopy-widget' as const

/**
 * One thing the host's page can be asked to do.
 *
 * `parameters` is JSON-Schema and is the reason this is a spec rather than a
 * bare name: an agent cannot call `dismissItems` without being told it takes
 * `{ids: number[]}`. Names alone force the agent to learn the call shape from
 * prose, which is exactly what a tool schema exists to prevent.
 */
export interface ActionSpec {
  name: string
  description?: string
  /** JSON-Schema for the arguments object. */
  parameters?: Record<string, unknown>
}

/** frame → host */
export type FrameMessage =
  /** "I exist" — the only message posted before origins are known, and the only
   *  one that carries nothing. See the shell in apps/tokens/views_embed.py. */
  | { source: typeof SOURCE; type: 'ready' }
  /** The frame's token was rejected or is near expiry; mint another. `grant: true`
   *  asks the host for an act-as-you permission (a host grant) with it: the frame
   *  sets it only once the person has sent a message in the panel (and until they
   *  press Stop), so a page never grants on load. Absent/false = no grant. */
  | { source: typeof SOURCE; type: 'token-request'; id: string; grant?: boolean }
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
  /** Data the host page is showing has changed; it should re-read.
   *
   *  Carries the resource URI and nothing else — MCP's
   *  `notifications/resources/updated` shape. The host re-reads through the path
   *  it already uses, where its own authorization applies; a diff would be a
   *  second source of truth for data the page already knows how to load. */
  | { source: typeof SOURCE; type: 'invalidate'; resource: string }
  /** The frame wants the panel closed (the user hit its close button). */
  | { source: typeof SOURCE; type: 'close' }
  /** Desired panel height in `inline` mode, where the host owns layout. */
  | { source: typeof SOURCE; type: 'resize'; height: number }
  /** The user pressed the panel's expand control (or Esc while expanded).
   *  The host decides: it answers with `display` either way, so the frame's
   *  button always shows what is actually on screen. */
  | { source: typeof SOURCE; type: 'expand'; expanded: boolean }
  /** The user pressed the docked rail's snap control: reserve its width in the
   *  page, or float over it again. Answered with `display`, like `expand`. */
  | { source: typeof SOURCE; type: 'snap'; snapped: boolean }
  /** The conversation open in the panel changed: one was started or opened, or
   *  the panel went back to its start screen (`null`). A POINTER, never content —
   *  see `PanelSession`. */
  | { source: typeof SOURCE; type: 'session'; session: PanelSession | null }

/**
 * The conversation the panel has open, as the frame reports it to the host page.
 *
 * Exactly these three facts and nothing it said: no title, no opening words, no
 * messages. A private conversation's words are sealed to its people, and the
 * page around the panel is not one of them — so the host learns that a
 * conversation is open and where it began, which is enough to keep a pointer to
 * it (`openSession` brings it back), and nothing a pointer could leak.
 */
export interface PanelSession {
  /** canopy's id for the conversation. */
  id: string
  /** The page it was STARTED on: the `resource` its page first declared, as canopy
   *  recorded it (`origin_resource`), "" if none. Not where it is open now. */
  resource: string
  /** A private (sensitive) conversation: readable only by its people. */
  private: boolean
}

/** host → frame */
export type HostMessage =
  | {
      source: typeof SOURCE
      type: 'init'
      token: string
      /** When `token` expires (ISO-8601), as the host's mint said; "" if it did not. */
      expires_at?: string
      agent?: string
      metadata?: Record<string, unknown>
      actions: ActionSpec[]
      /** Validated host theme for the panel's contents. The frame validates it
       *  AGAIN — it is canopy's document, and does not take a host's word for
       *  what may be written into its styles. */
      theme?: SafeTheme
      /** Whether the panel may be expanded — the frame draws its expand
       *  control only when this is true. Absent (an older loader) = false. */
      expandable?: boolean
      /** Whether the panel is expanded right now (a remembered choice). */
      expanded?: boolean
      /** Whether the rail may be snapped into the page (docked only). Absent
       *  (an older loader) = false. */
      snappable?: boolean
      /** Whether it is snapped right now (a remembered choice). */
      snapped?: boolean
    }
  /** The panel's size changed: expanded (most of the viewport) or not. Sent
   *  after every `expand` and whenever the host expands or collapses itself. */
  | { source: typeof SOURCE; type: 'display'; expanded: boolean; snapped?: boolean }
  | { source: typeof SOURCE; type: 'token'; id: string; token: string; expires_at?: string }
  | { source: typeof SOURCE; type: 'token-error'; id: string; message: string }
  | { source: typeof SOURCE; type: 'context'; id: string; context: Record<string, unknown> }
  | { source: typeof SOURCE; type: 'action-result'; id: string; result: unknown }
  | { source: typeof SOURCE; type: 'action-error'; id: string; message: string }
  /** The set of callable actions changed while the panel was open. */
  | { source: typeof SOURCE; type: 'actions'; actions: ActionSpec[] }
  /** The page's VIEW changed while the panel was open.
   *
   *  Pushed, not polled, and that is the point: the old `context` reply was
   *  answered once when the frame asked, so a user who filtered the page after
   *  opening the chat left the agent holding a screen that no longer existed.
   *  This is the same shape as `actions` above — the host tells the frame when
   *  the thing it declared has changed — because the two are the same kind of
   *  fact about a page: what it can do, and what it is showing. */
  | { source: typeof SOURCE; type: 'state'; state: Record<string, unknown> }
  | { source: typeof SOURCE; type: 'visibility'; open: boolean }
  /** The host re-themed (e.g. its own light/dark toggle) while the panel is up. */
  | { source: typeof SOURCE; type: 'theme'; theme: SafeTheme }
  /** The page asks the agent a question on the visitor's behalf — a button such
   *  as "Explain this answer". The frame starts a conversation with this text as
   *  the visitor's first message, so it is shown to them exactly as sent. */
  | { source: typeof SOURCE; type: 'ask'; text: string }
  /** Open this conversation in the panel (`widget.openSession`). The frame reads
   *  it AS THE VISITOR, so canopy's session ACL decides: one they are not in shows
   *  them canopy's own answer (a private one: ask to be added), never its words. */
  | { source: typeof SOURCE; type: 'open-session'; id: string }

/** Longest question a host may send. A button's question is a sentence; this
 *  stops a page pasting a document into somebody's conversation. */
export const MAX_ASK_CHARS = 2000

export function isFrameMessage(data: unknown): data is FrameMessage {
  if (typeof data !== 'object' || data === null) return false
  const m = data as Record<string, unknown>
  if (m.source !== SOURCE || typeof m.type !== 'string') return false
  // Only kinds the FRAME may send. A host-bound message ('init', 'token', …)
  // arriving here is not something to interpret — the host is the only party
  // that issues those, so seeing one means something is impersonating it.
  return [
    'ready', 'token-request', 'context-request', 'action-request', 'invalidate', 'close', 'resize', 'expand', 'snap',
    'session',
  ].includes(m.type)
}

/** The origin half of a `baseUrl`, which is the only value we will postMessage
 *  to or accept messages from.
 *
 *  A relative base (`/canopy`, the same-origin deployment) resolves against the
 *  current page, which is correct: the frame really is same-origin then. */
/** A canopy session id: a UUID. The host's `openSession` and the frame's report
 *  both hold to it, so neither end passes on something that is not one. */
export function isSessionId(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
}

/** A frame's `session` report, checked: the three fields `PanelSession` names and
 *  nothing else, or `null`. Anything malformed is `undefined` (drop the message). */
export function panelSessionOf(value: unknown): PanelSession | null | undefined {
  if (value === null) return null
  if (typeof value !== 'object' || value === undefined) return undefined
  const v = value as Record<string, unknown>
  if (!isSessionId(v.id)) return undefined
  return {
    id: v.id,
    resource: typeof v.resource === 'string' ? v.resource.slice(0, 500) : '',
    private: v.private === true,
  }
}

export function originOf(baseUrl: string, pageOrigin: string): string {
  return new URL(baseUrl, pageOrigin).origin
}

/** Read one cookie out of a `document.cookie` string.
 *
 *  Pure (the string is passed in) so the name-matching is unit-testable without
 *  a DOM, and so the name can be escaped in one place: a cookie name is host
 *  configuration, and interpolating it straight into a `RegExp` lets a dot or a
 *  `+` in it match something it should not.
 */
export function readCookie(cookieString: string, name: string): string {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = new RegExp(`(?:^|;\\s*)${escaped}=([^;]*)`).exec(cookieString)
  return match ? decodeURIComponent(match[1]) : ''
}
