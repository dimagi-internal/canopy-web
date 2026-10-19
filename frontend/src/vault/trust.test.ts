// Who a sealed value goes to (spec 2026-10-19-give-an-agent-a-secret-design.md §4):
// only recipients endorsed by a key this browser trusts. Each case is an attack
// a compromised canopy-web could try with the recipients list.
import { describe, expect, it } from 'vitest'
import type { ZkRecipientsOut } from '@/api/vault'
import { assess, changedKey, newEndorsers } from './trust'
import { endorse, generateKeyPair, jkt, type PublicJwk } from './zk'

const AGENT = '42'

async function person(userId: string) {
  const k = await generateKeyPair()
  return { userId, ...k, jkt: await jkt(k.publicJwk) }
}

async function device() {
  const k = await generateKeyPair()
  return { publicJwk: k.publicJwk, jkt: await jkt(k.publicJwk) }
}

type Person = Awaited<ReturnType<typeof person>>
type Device = Awaited<ReturnType<typeof device>>

function recipients(devices: Device[], members: Person[]) {
  return [
    ...devices.map((d) => ({ kind: 'device' as const, target: d.jkt, label: 'box', public_jwk: d.publicJwk, key_jkt: d.jkt })),
    ...members.map((m) => ({ kind: 'member' as const, target: m.userId, label: `${m.userId}@x`, public_jwk: m.publicJwk,
                             key_jkt: m.jkt })),
  ]
}

async function vouch(by: Person, kind: 'device' | 'member', target: string, pub: PublicJwk, signerPub?: PublicJwk) {
  const e = await endorse(by.scalar, by.publicJwk, AGENT, kind, target, pub)
  return { ...e, by: by.userId, by_email: `${by.userId}@x`, by_public_jwk: signerPub ?? by.publicJwk }
}

function data(r: ReturnType<typeof recipients>, endorsements: Awaited<ReturnType<typeof vouch>>[]): ZkRecipientsOut {
  return { agent: 'bailey', agent_id: AGENT, kv: 0, recipients: r, endorsements, declared: ['GITHUB_TOKEN'],
           stored: [] } as unknown as ZkRecipientsOut
}

describe('assess — who a sealed value may go to', () => {
  it('with my own key: my endorsed machine and myself, nothing else', async () => {
    const [jj, box, rogue] = [await person('7'), await device(), await device()]
    const d = data(recipients([box, rogue], [jj]), [await vouch(jj, 'device', box.jkt, box.publicJwk)])
    const a = await assess(d, { self: { userId: '7', publicJwk: jj.publicJwk } })
    expect(a.sealTo.map((r) => `${r.kind}:${r.target}`).sort()).toEqual([`device:${box.jkt}`, 'member:7'].sort())
    expect(a.unconfirmed.map((r) => r.target)).toEqual([rogue.jkt])  // an injected key gets nothing
    expect(a.reachesAMachine).toBe(true)
    expect(a.endorsers[0].status).toBe('self')
  })

  it('a stranger endorsing a key is not enough: new until accepted, then sealed to', async () => {
    const [jj, box] = [await person('7'), await device()]
    const d = data(recipients([box], [jj]), [await vouch(jj, 'device', box.jkt, box.publicJwk)])
    const a = await assess(d)
    expect(a.sealTo).toEqual([])
    expect(newEndorsers(a).map((e) => [e.by, e.jkt])).toEqual([['7', jj.jkt]])
    const b = await assess(d, { accept: new Set(['7']) })
    expect(b.sealTo.map((r) => r.kind)).toEqual(['device'])
  })

  it('a pinned person whose key changed stops everything', async () => {
    const [jj, box, impostor] = [await person('7'), await device(), await person('7')]
    const d = data(recipients([box], []), [await vouch(impostor, 'device', box.jkt, box.publicJwk)])
    const a = await assess(d, { pins: { '7': { jkt: jj.jkt, email: '7@x', pinned_at: '' } } })
    expect(changedKey(a)?.was).toBe(jj.jkt)
    expect(a.sealTo).toEqual([])
  })

  it('the server substituting MY key is caught too', async () => {
    const [me, box, impostor] = [await person('7'), await device(), await person('7')]
    const d = data(recipients([box], []), [await vouch(impostor, 'device', box.jkt, box.publicJwk)])
    const a = await assess(d, { self: { userId: '7', publicJwk: me.publicJwk } })
    expect(changedKey(a)?.status).toBe('changed')
    expect(a.sealTo).toEqual([])
  })

  it('a recipient whose key does not match its thumbprint is never sealed to', async () => {
    const [jj, box, other] = [await person('7'), await device(), await device()]
    const r = recipients([box], [])
    r[0] = { ...r[0], public_jwk: other.publicJwk }  // claims box's jkt, carries another key
    const d = data(r, [await vouch(jj, 'device', box.jkt, box.publicJwk)])
    const a = await assess(d, { self: { userId: '7', publicJwk: jj.publicJwk } })
    expect(a.sealTo).toEqual([])
    expect(a.unconfirmed[0].why).toMatch(/thumbprint/)
  })

  it('a forged signature (right signer named, wrong key signed) is ignored', async () => {
    const [jj, box, forger] = [await person('7'), await device(), await person('9')]
    // Signed by the forger, presented as jj's (by_public_jwk = jj's key).
    const e = await vouch(forger, 'device', box.jkt, box.publicJwk, jj.publicJwk)
    const d = data(recipients([box], [jj]), [{ ...e, by: '7', by_jkt: jj.jkt }])
    const a = await assess(d, { self: { userId: '7', publicJwk: jj.publicJwk } })
    expect(a.sealTo.map((r) => r.kind)).toEqual(['member'])  // only my own key
    expect(a.reachesAMachine).toBe(false)
  })
})
