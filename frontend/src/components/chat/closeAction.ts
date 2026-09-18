import type { ChatSession, CloseResult } from '@/api/chat'

/**
 * Can this session be closed right now, and should we ask first?
 *
 * Pure and tested without a component, mirroring `chatPageLogic.ts::sendBlockReason`
 * — the decision is the part worth testing, not the markup around it.
 *
 * FAILS OPEN, deliberately: `runner_online === null` means UNBOUND (a cloud session,
 * or a web chat that has never sent), and those close server-side and always
 * succeed. Treating unknown as blocked would disable the button on exactly the
 * sessions closing is guaranteed to work for.
 */
export type CloseSubject = Pick<
  ChatSession,
  'status' | 'running' | 'runner_online' | 'runner_status' | 'runner_name'
>

export type CloseIntent =
  | { kind: 'ready'; confirm: boolean }
  | { kind: 'blocked'; why: string }

export function closeIntent(s: CloseSubject): CloseIntent {
  if (s.status !== 'active') return { kind: 'blocked', why: 'Already closed' }
  if (s.runner_online === false) {
    const box = s.runner_name ?? 'its runner'
    const why = s.runner_status ?? 'offline'
    return { kind: 'blocked', why: `Can't close — ${box} is ${why}` }
  }
  // Mid-turn is not a block. A session stuck in a loop is precisely when you most
  // want it gone; it just gets one confirmation first.
  return { kind: 'ready', confirm: Boolean(s.running) }
}

/**
 * What to tell the user afterwards, or null for "say nothing".
 *
 * `already_closed` is a success from where the user sits: they wanted it gone and
 * it is gone. It comes back `ok:false` because the API describes what it did, not
 * how the user feels about it — the translation belongs here.
 */
export function closeResultMessage(r: CloseResult, s: CloseSubject): string | null {
  if (r.ok) return null
  if (r.reason === 'already_closed') return null
  if (r.reason === 'unavailable') {
    const box = s.runner_name ?? 'its runner'
    return `Could not close — ${box} is ${s.runner_status ?? 'offline'}`
  }
  return "Could not close this session"
}

/**
 * Where to go once a session is closed: back where you came from.
 *
 * React Router stamps `idx` on every entry it pushes, so `idx > 0` means the
 * previous entry is a page of this app, and going back returns you there (the
 * session list, a board, an agent page). A chat opened straight from a link or
 * a notification has nothing of ours behind it. Going back there would leave
 * the app, so it falls back to the workspace's chat list.
 */
export function closeDestination(historyIdx: unknown, workspace: string): -1 | string {
  return typeof historyIdx === 'number' && historyIdx > 0 ? -1 : `/w/${workspace}/chat`
}

/** How often, and for how long, the page checks whether a close relayed to a
 *  runner has landed. The runner retires the session on its next report
 *  (~10s), so 45s covers a few missed cycles without leaving you stuck on a
 *  page that will never change. */
export const CLOSE_POLL_MS = 2_000
export const CLOSE_WAIT_MS = 45_000
