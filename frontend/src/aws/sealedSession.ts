/**
 * Making a sealed grant's AWS session, in the approver's browser (`apps/agents/aws_sealed.py`).
 *
 * 1. Open the role's key, sealed to the approver's own vault key (their passkey touch unlocked it).
 * 2. Decide which of the agent's machines to seal to: only device keys endorsed by someone this
 *    browser trusts (`trust.assess`) — canopy-web's list alone is never enough, so a canopy-web
 *    that names a key of its own gets nothing. A person not trusted yet is shown, with their key's
 *    fingerprint, for the approver to accept.
 * 3. Ask STS for the session as the agent, straight from this browser (`sts.ts`).
 * 4. Seal the session to those machines. canopy-web stores only that ciphertext.
 */
import type { AwsSealing } from '@/api/awsSealed'
import type { ZkRecipientsOut } from '@/api/vault'
import { assess, changedKey, loadPins, newEndorsers, type Endorser } from '@/vault/trust'
import type { Unlocked } from '@/vault/userKey'
import { openSealedValue, sealValue, type Recipient } from '@/vault/zk'
import { assumeRole, type AwsKey, type StsRequest } from './sts'

export type Minted =
  | { kind: 'sealed'; valueZk: string; expiresAt: string; machines: number }
  | { kind: 'trust'; people: Endorser[] }

export class SealedSessionError extends Error {}

/** The machines to seal to, or the people the approver must decide about first. */
export async function machinesToSealTo(machines: ZkRecipientsOut, me: Unlocked, accept: Set<string>):
    Promise<Recipient[] | Endorser[]> {
  const a = await assess(machines, { self: { userId: me.userId, publicJwk: me.publicJwk }, pins: loadPins(), accept })
  const changed = changedKey(a)
  if (changed) {
    throw new SealedSessionError(`${changed.email}'s vault key changed since this browser last saw it. `
      + 'Confirm the new key with them before sealing anything to the machines they vouch for.')
  }
  const devices = a.sealTo.filter((r) => r.kind === 'device')
  if (devices.length) return devices
  const people = newEndorsers(a)
  if (people.length) return people
  throw new SealedSessionError('No machine that runs this agent has a key anyone you trust has vouched for, so '
    + 'its keys cannot be sealed to it. An admin of the agent vouches for its machines on the agent\'s vault page.')
}

export async function mintSealedSession(sealing: AwsSealing, me: Unlocked, accept = new Set<string>(),
                                        fetcher: typeof fetch = fetch.bind(globalThis)): Promise<Minted> {
  const to = await machinesToSealTo(sealing.machines as unknown as ZkRecipientsOut, me, accept)
  if (to.length && 'by' in to[0]) return { kind: 'trust', people: to as Endorser[] }
  let key: AwsKey
  try {
    key = JSON.parse(await openSealedValue(sealing.key_value_zk, sealing.key_binding, sealing.key_name,
                                           'member', me.userId, me.privateKey)) as AwsKey
  } catch {
    throw new SealedSessionError(`Your vault key does not open ${sealing.role}'s sealed key. `
      + 'Someone who holds it re-registers it for you (the AWS keys page).')
  }
  const session = await assumeRole(key, sealing.sts as unknown as StsRequest, fetcher)
  if (sealing.grant_expires_at && new Date(session.Expiration) > new Date(new Date(sealing.grant_expires_at).getTime() + 300_000)) {
    throw new SealedSessionError('AWS made a session that outlasts the grant; ask again.')
  }
  const valueZk = await sealValue(JSON.stringify(session), sealing.session_binding, sealing.session_name,
                                  sealing.kv ?? 0, to as Recipient[])
  return { kind: 'sealed', valueZk, expiresAt: session.Expiration, machines: to.length }
}
