// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AwsSealing } from '@/api/awsSealed'
import type { Unlocked } from '@/vault/userKey'
import { endorse, generateKeyPair, jkt, openSealedValue, sealValue } from '@/vault/zk'
import { mintSealedSession, SealedSessionError } from './sealedSession'

const OK = `<AssumeRoleResponse><AssumeRoleResult><Credentials><AccessKeyId>ASIAMINTED</AccessKeyId>
<SecretAccessKey>s</SecretAccessKey><SessionToken>t</SessionToken><Expiration>2030-01-01T01:00:00Z</Expiration>
</Credentials></AssumeRoleResult></AssumeRoleResponse>`

// The whole browser half with real keys: the approver's vault key, a machine's key they vouched
// for (or someone else did), the role key sealed to the approver, STS faked at the network.
async function world(opts: { endorsedBy?: 'me' | 'stranger' | 'nobody' } = {}) {
  const me = await generateKeyPair()
  const stranger = await generateKeyPair()
  const machine = await generateKeyPair()
  const agentId = '42'
  const machineJkt = await jkt(machine.publicJwk)
  const unlocked: Unlocked = { userId: '7', privateKey: me.privateKey, publicJwk: me.publicJwk, scalar: me.scalar } as Unlocked
  const endorser = opts.endorsedBy === 'stranger' ? stranger : me
  const endorsements = opts.endorsedBy === 'nobody' ? [] : [{
    ...(await endorse(endorser.scalar, endorser.publicJwk, agentId, 'device', machineJkt, machine.publicJwk)),
    by: opts.endorsedBy === 'stranger' ? '9' : '7', by_email: opts.endorsedBy === 'stranger' ? 'bo@dimagi.com' : 'jj@dimagi.com',
    by_public_jwk: endorser.publicJwk,
  }]
  const key = await sealValue(JSON.stringify({ AccessKeyId: 'AKIAROLEKEY', SecretAccessKey: 'role-secret' }),
                              'aws-role:labs-readwrite', 'AWS_ACCESS_KEY', 0,
                              [{ kind: 'member', target: '7', publicJwk: me.publicJwk }])
  const sealing = {
    grant: 5, role: 'labs-readwrite', key_value_zk: key, key_binding: 'aws-role:labs-readwrite', key_name: 'AWS_ACCESS_KEY',
    kv: 0, session_binding: 'aws-grant:5', session_name: 'AWS_SESSION', grant_expires_at: '2030-01-01T02:00:00Z',
    sts: { role_arn: 'arn:aws:iam::1:role/labs-jj-agent-readwrite', role_session_name: 'canopy-ace',
           source_identity: 'canopy-agent-ace', tags: [{ Key: 'agent', Value: 'ace' }], duration_seconds: 3600,
           region: 'us-east-1' },
    machines: { agent: 'ace', agent_id: agentId, kv: 0, declared: [], stored: [], endorsements,
                recipients: [{ kind: 'device', target: machineJkt, label: 'box', public_jwk: machine.publicJwk,
                               key_jkt: machineJkt }] },
  } as unknown as AwsSealing
  return { unlocked, machine, machineJkt, sealing }
}

beforeEach(() => localStorage.clear())

describe('a sealed AWS session', () => {
  it('opens the role key, asks STS with it, and seals the session to the vouched machine only', async () => {
    const w = await world()
    const fetcher = vi.fn().mockResolvedValue(new Response(OK))
    const minted = await mintSealedSession(w.sealing, w.unlocked, new Set(), fetcher as unknown as typeof fetch)
    expect(minted.kind).toBe('sealed')
    if (minted.kind !== 'sealed') return
    // STS was signed with the ROLE key the vault opened, and nothing was sent to canopy-web.
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher.mock.calls[0][0]).toBe('https://sts.amazonaws.com/')
    expect(fetcher.mock.calls[0][1].headers.authorization).toContain('Credential=AKIAROLEKEY/')
    // The machine opens it with its own key.
    const opened = JSON.parse(await openSealedValue(minted.valueZk, 'aws-grant:5', 'AWS_SESSION', 'device',
                                                    w.machineJkt, w.machine.privateKey))
    expect(opened).toMatchObject({ AccessKeyId: 'ASIAMINTED', SessionToken: 't' })
    expect(minted.valueZk).not.toContain('role-secret')
  })

  it('asks first about a person this browser has not trusted, and seals once they are accepted', async () => {
    const w = await world({ endorsedBy: 'stranger' })
    const fetcher = vi.fn().mockResolvedValue(new Response(OK))
    const first = await mintSealedSession(w.sealing, w.unlocked, new Set(), fetcher as unknown as typeof fetch)
    expect(first).toMatchObject({ kind: 'trust', people: [{ email: 'bo@dimagi.com' }] })
    expect(fetcher).not.toHaveBeenCalled()   // no keys made before the machines are decided
    const second = await mintSealedSession(w.sealing, w.unlocked, new Set(['9']), fetcher as unknown as typeof fetch)
    expect(second.kind).toBe('sealed')
  })

  it('never seals to a machine nobody vouched for', async () => {
    const w = await world({ endorsedBy: 'nobody' })
    const fetcher = vi.fn()
    await expect(mintSealedSession(w.sealing, w.unlocked, new Set(), fetcher as unknown as typeof fetch))
      .rejects.toBeInstanceOf(SealedSessionError)
    expect(fetcher).not.toHaveBeenCalled()
  })
})
