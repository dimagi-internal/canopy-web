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
