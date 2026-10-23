// Replacing a vault key keeps its person's private conversations: every copy they hold
// is opened with the OLD key and stored wrapped to the NEW one. The real crypto runs;
// only the REST calls are mocked.
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ myKeyCopies: vi.fn(), putMyKeyCopies: vi.fn() }))
vi.mock('@/api/chat', () => api)
vi.stubGlobal('window', { localStorage: { getItem: () => null, setItem: () => undefined, removeItem: () => undefined } })

const { atStake, replaceKeepingConversations } = await import('./conversationCopies')
const { wrapInfo } = await import('./sealedChat')
const zk = await import('./zk')

afterEach(() => vi.clearAllMocks())

async function copyFor(conversation: string, sessionId: string, scalar: Uint8Array, to: { x?: string; y?: string }) {
  return { session_id: sessionId, conversation, public_jwk: {}, recipient_jkt: await zk.jkt(to),
           wrap: await zk.wrapEcdhEs(scalar, to, wrapInfo(conversation, '7')) as unknown as Record<string, unknown>,
           agent_name: 'Labs Data', workspace: 'connect', started_at: '2026-10-20T10:00:00Z' }
}

describe('replaceKeepingConversations', () => {
  it('carries every copy the old key opens to the new key, and names what it could not', async () => {
    const old = await zk.generateKeyPair()
    const fresh = await zk.generateKeyPair()
    const conv = await zk.generateKeyPair()
    const stranger = await zk.generateKeyPair()
    const held = [await copyFor('conv-a', 's-a', conv.scalar, old.publicJwk),
                  await copyFor('conv-b', 's-b', zk.randomBytes(32), stranger.publicJwk)]
    api.putMyKeyCopies.mockResolvedValue({ stored: 1 })
    const order: string[] = []
    const out = await replaceKeepingConversations(
      async () => { order.push('unlock old'); return { userId: '7', privateKey: old.privateKey, publicJwk: old.publicJwk } },
      async () => {
        order.push('make new')
        return { keyset: { exists: true } as never, me: { userId: '7', privateKey: fresh.privateKey, publicJwk: fresh.publicJwk } }
      },
      held as never,
    )
    expect(order).toEqual(['unlock old', 'make new'])
    expect(out.carried).toBe(1)
    expect(out.lost.map((c) => c.session_id)).toEqual(['s-b'])
    const [[copies]] = api.putMyKeyCopies.mock.calls
    expect(copies).toHaveLength(1)
    expect(copies[0].recipient_jkt).toBe(await zk.jkt(fresh.publicJwk))
    // Only the NEW key opens the stored copy, to the same conversation key.
    const opened = await zk.unwrapEcdhEs(copies[0].wrap, fresh.privateKey, fresh.publicJwk, wrapInfo('conv-a', '7'))
    expect(Array.from(opened)).toEqual(Array.from(conv.scalar))
    await expect(zk.unwrapEcdhEs(copies[0].wrap, old.privateKey, old.publicJwk, wrapInfo('conv-a', '7'))).rejects.toThrow()
  })

  it('retries a failed store, and says what to do if it never lands', async () => {
    const old = await zk.generateKeyPair()
    const fresh = await zk.generateKeyPair()
    const conv = await zk.generateKeyPair()
    const held = [await copyFor('conv-a', 's-a', conv.scalar, old.publicJwk)]
    api.putMyKeyCopies.mockRejectedValueOnce(new Error('502')).mockResolvedValue({ stored: 1 })
    const run = () => replaceKeepingConversations(
      async () => ({ userId: '7', privateKey: old.privateKey, publicJwk: old.publicJwk }),
      async () => ({ keyset: {} as never, me: { userId: '7', privateKey: fresh.privateKey, publicJwk: fresh.publicJwk } }),
      held as never)
    expect((await run()).carried).toBe(1)
    expect(api.putMyKeyCopies).toHaveBeenCalledTimes(2)
  }, 20_000)

  it('asks for no unlock when there is nothing to carry', async () => {
    const fresh = await zk.generateKeyPair()
    const unlockOld = vi.fn()
    await replaceKeepingConversations(unlockOld,
      async () => ({ keyset: {} as never, me: { userId: '7', privateKey: fresh.privateKey, publicJwk: fresh.publicJwk } }), [])
    expect(unlockOld).not.toHaveBeenCalled()
    expect(api.putMyKeyCopies).not.toHaveBeenCalled()
  })

  it('names the private conversations at stake for the confirm', () => {
    const text = atStake([{ agent_name: 'Labs Data', started_at: '2026-10-20T10:00:00Z' } as never])
    expect(text).toContain('1 private conversation')
    expect(text).toContain('with Labs Data')
    expect(atStake([])).toBe('')
  })
})
