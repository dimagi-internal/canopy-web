import { describe, expect, it, vi } from 'vitest'

import { resolvePrincipal } from './principal'

class Refused extends Error {
  status: number
  constructor(status: number) {
    super(String(status))
    this.status = status
  }
}

const AGENTS = [{ slug: 'echo', name: 'Echo', description: '' }]

describe('working out who is behind the frame', () => {
  it('is a user when the embed surface answers', async () => {
    const json = vi.fn(async () => AGENTS)
    await expect(resolvePrincipal(json as never)).resolves.toEqual({
      kind: 'user',
      agents: AGENTS,
    })
    // The common case today pays nothing extra.
    expect(json).toHaveBeenCalledTimes(1)
  })

  it('falls through to the contact surface on a refusal', async () => {
    const json = vi.fn(async (path: string) => {
      if (path === '/api/embed/agents') throw new Refused(401)
      return { contact_id: 7, display_name: 'Amina', app: 'connect-labs', agents: AGENTS }
    })

    await expect(resolvePrincipal(json as never)).resolves.toEqual({
      kind: 'contact',
      contactId: 7,
      displayName: 'Amina',
      app: 'connect-labs',
      agents: AGENTS,
    })
  })

  it('treats 403 as a mismatch too', async () => {
    // `/api/embed/agents` answers 403 when the token carries no acting app,
    // and 401 when the login middleware refuses it. Both mean "not a user
    // token", and only one of them was obvious.
    const json = vi.fn(async (path: string) => {
      if (path === '/api/embed/agents') throw new Refused(403)
      return { contact_id: 1, display_name: '', app: 'x', agents: [] }
    })

    await expect(resolvePrincipal(json as never)).resolves.toMatchObject({ kind: 'contact' })
  })

  it('does not swallow a real failure as "must be a contact"', async () => {
    const json = vi.fn(async () => {
      throw new Refused(500)
    })
    await expect(resolvePrincipal(json as never)).rejects.toThrow('500')
    expect(json).toHaveBeenCalledTimes(1)
  })

  it('surfaces the contact surface refusing as well, rather than an empty list', async () => {
    const json = vi.fn(async () => {
      throw new Refused(401)
    })
    await expect(resolvePrincipal(json as never)).rejects.toThrow('401')
    expect(json).toHaveBeenCalledTimes(2)
  })
})
