import { describe, expect, it } from 'vitest'
import { closeIntent, closeResultMessage } from './closeAction'

const base = {
  status: 'active',
  running: false,
  runner_online: true,
  runner_status: 'online',
  runner_name: 'jj-mbp',
} as const

describe('closeIntent', () => {
  it('is ready without confirmation for an idle session', () => {
    expect(closeIntent(base)).toEqual({ kind: 'ready', confirm: false })
  })

  it('asks first when the agent is mid-turn', () => {
    expect(closeIntent({ ...base, running: true })).toEqual({ kind: 'ready', confirm: true })
  })

  it('blocks when the bound runner cannot act, naming the box and why', () => {
    const intent = closeIntent({ ...base, runner_online: false, runner_status: 'paused' })
    expect(intent.kind).toBe('blocked')
    expect(intent.kind === 'blocked' && intent.why).toContain("jj-mbp")
    expect(intent.kind === 'blocked' && intent.why).toContain("paused")
  })

  it('blocks an already-closed session', () => {
    expect(closeIntent({ ...base, status: 'archived' }).kind).toBe('blocked')
  })

  it('FAILS OPEN when liveness is merely unknown', () => {
    // runner_online: null means unbound — a cloud session or a web chat that has
    // never sent. Those close server-side and always work. Blocking them would
    // disable the button on exactly the sessions closing is guaranteed to fix.
    expect(closeIntent({ ...base, runner_online: null, runner_status: null }))
      .toEqual({ kind: 'ready', confirm: false })
  })
})

describe('closeResultMessage', () => {
  it('says nothing on success', () => {
    expect(closeResultMessage({ ok: true, closing: true, reason: '' }, base)).toBeNull()
  })

  it('treats an already-closed race as done, not as an error', () => {
    // Double-tap, or someone else closed it first. The row is going away either
    // way; an error toast would be noise about a wish that came true.
    expect(closeResultMessage({ ok: false, closing: false, reason: 'already_closed' }, base))
      .toBeNull()
  })

  it('explains an unreachable runner in terms of the box', () => {
    const msg = closeResultMessage({ ok: false, closing: false, reason: 'unavailable' }, base)
    expect(msg).toContain('jj-mbp')
  })
})
