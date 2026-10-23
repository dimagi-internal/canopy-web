// The keepers' record a browser writes when an owner or admin grants someone: it must
// open with the vault key, in the CLI's format ({v, people: {uid: {jkt, ...}}}), merge
// with what is there, and retry once over a newer version.
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/problem'
import type { ZkStateOut } from '@/api/vault'
import { recordGranted } from './operations'
import { decryptValue, encryptValue, generateKeyPair, jkt, randomBytes } from './zk'

const NAME = '~keeper-pins'

async function person() {
  return (await generateKeyPair()).publicJwk as unknown as Record<string, unknown>
}

function state(over: Partial<ZkStateOut> = {}): ZkStateOut {
  return { agent: 'ace', agent_id: '42', enabled: true, kv: 1, wraps: [], pending: [],
           pins: { value: '', kv: 0, version: 0 }, ...over } as ZkStateOut
}

describe('recordGranted', () => {
  it('records each person granted to, merged with the record already there', async () => {
    const vk = randomBytes(32)
    const old = await encryptValue(vk, '42', NAME, 1, JSON.stringify({ v: 1, people: { 7: { jkt: 'jj' } } }))
    const ed = await person()
    type Body = { kv: number; expected_version: number; value: string }
    const put = vi.fn<(body: Body) => Promise<object>>(async () => ({}))
    const ok = await recordGranted(state({ pins: { value: old, kv: 1, version: 4 } }), vk,
      [{ kind: 'member', target: '9', public_jwk: ed, email: 'ed@dimagi.com' } as never], put, async () => state())
    expect(ok).toBe(true)
    const body = put.mock.calls[0][0]
    expect([body.kv, body.expected_version]).toEqual([1, 4])
    const people = JSON.parse(await decryptValue(vk, '42', NAME, 1, body.value)).people
    expect(people['7']).toEqual({ jkt: 'jj' })
    expect(people['9'].jkt).toBe(await jkt(ed as never))
  })

  it('reads again and retries once when another writer got there first', async () => {
    const vk = randomBytes(32)
    const put = vi.fn()
      .mockRejectedValueOnce(new ApiError(409, 'stale', { error: 'keeper_pins_stale' }))
      .mockResolvedValueOnce({})
    const reread = vi.fn(async () => state({ pins: { value: '', kv: 0, version: 5 } }))
    const ok = await recordGranted(state(), vk,
      [{ kind: 'member', target: '9', public_jwk: await person() } as never], put, reread)
    expect(ok).toBe(true)
    expect(reread).toHaveBeenCalledOnce()
    expect((put.mock.calls[1][0] as { expected_version: number }).expected_version).toBe(5)
  })

  it('leaves a record that does not open alone, and ignores machines', async () => {
    const forged = await encryptValue(randomBytes(32), '42', NAME, 1, '{}')
    const put = vi.fn()
    expect(await recordGranted(state({ pins: { value: forged, kv: 1, version: 2 } }), randomBytes(32),
      [{ kind: 'member', target: '9', public_jwk: await person() } as never], put, async () => state())).toBe(false)
    expect(await recordGranted(state(), randomBytes(32),
      [{ kind: 'device', target: 'dk', public_jwk: await person() } as never], put, async () => state())).toBe(false)
    expect(put).not.toHaveBeenCalled()
  })
})
