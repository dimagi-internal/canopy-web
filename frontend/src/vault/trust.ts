/**
 * Who a sealed value may go to — decided HERE, not by canopy-web.
 *
 * Spec: docs/superpowers/specs/2026-10-19-give-an-agent-a-secret-design.md §4.
 * `GET …/vault/zk/recipients` is the server's claim about who may read an
 * agent. A writer who sealed to it as-is would hand any key a compromised
 * database inserted every value written afterwards. So a recipient is sealed to
 * only when it carries a valid zk-v1 §7 endorsement by a key this browser
 * already trusts:
 *
 *  1. the writer's OWN vault key, opened in this flow (`self`) — the server
 *     cannot substitute it, because the writer derived it from their passkey;
 *  2. a person pinned in this browser before (`pinned`, keyed by person);
 *  3. a person the writer accepts NOW, having been shown their fingerprint
 *     (`accept`, first contact — then pinned).
 *
 * A pinned person whose key CHANGED stops everything (`changed`): that is what
 * a substituted key looks like, and the writer must confirm it out of band.
 * Thumbprints are recomputed here; the server's `key_jkt` is never trusted.
 */
import type { ZkEndorsementOut, ZkRecipientOut, ZkRecipientsOut } from '@/api/vault'
import { jkt, verifyEndorsement, type Recipient } from './zk'

type Jwk = { x?: string; y?: string }

export interface Pin { jkt: string; email: string; pinned_at: string }
export type Pins = Record<string, Pin>

const PINS = 'canopy-vault/v1/pins'

export function loadPins(): Pins {
  try {
    const raw = window.localStorage.getItem(PINS)
    const v = raw ? (JSON.parse(raw) as unknown) : {}
    return v && typeof v === 'object' ? (v as Pins) : {}
  } catch {
    return {}
  }
}

export function savePins(add: { by: string; jkt: string; email: string }[]): void {
  try {
    const pins = loadPins()
    for (const p of add) pins[p.by] = { jkt: p.jkt, email: p.email, pinned_at: new Date().toISOString() }
    window.localStorage.setItem(PINS, JSON.stringify(pins))
  } catch {
    // storage blocked: the person is asked again next time
  }
}

export type EndorserStatus = 'self' | 'pinned' | 'accepted' | 'new' | 'changed'

export interface Endorser {
  by: string
  email: string
  /** The thumbprint of the key that signed, recomputed here. */
  jkt: string
  status: EndorserStatus
  /** For `changed`: the thumbprint pinned before. */
  was?: string
  /** Recipients this person's valid endorsements cover, by `kind:target`. */
  covers: string[]
}

export interface Assessment {
  agentId: string
  kv: number
  /** Endorsed by a trusted key (or the writer's own member key): seal to these. */
  sealTo: Recipient[]
  /** Everyone else the server named, with why. */
  unconfirmed: (ZkRecipientOut & { why: string })[]
  endorsers: Endorser[]
  /** A device reads it: without one, no runner can until an admin settles it. */
  reachesAMachine: boolean
}

export interface Self { userId: string; publicJwk: Jwk }

const keyOf = (kind: string, target: string) => `${kind}:${target}`

export async function assess(data: ZkRecipientsOut, opts: { self?: Self | null; pins?: Pins; accept?: Set<string> } = {}):
    Promise<Assessment> {
  const pins = opts.pins ?? {}
  const accept = opts.accept ?? new Set<string>()
  const selfJkt = opts.self ? await jkt(opts.self.publicJwk) : ''
  // 1. Each recipient's REAL thumbprint; a row whose key does not match what it claims is dropped.
  const real = new Map<string, { r: ZkRecipientOut; jkt: string }>()
  const unconfirmed: Assessment['unconfirmed'] = []
  for (const r of data.recipients) {
    const k = await jkt(r.public_jwk as Jwk)
    if (k !== r.key_jkt || (r.kind === 'device' && r.target !== k)) {
      unconfirmed.push({ ...r, why: 'its key does not match its thumbprint' })
      continue
    }
    real.set(keyOf(r.kind, r.target), { r, jkt: k })
  }
  // 2. Valid endorsements, grouped by endorser, with each endorser's standing.
  const endorsers = new Map<string, Endorser>()
  for (const e of data.endorsements as ZkEndorsementOut[]) {
    const hit = real.get(keyOf(e.kind, e.target))
    if (!hit || hit.jkt !== e.key_jkt || !e.by_public_jwk) continue
    if (!(await verifyEndorsement(e, data.agent_id, e.by_public_jwk as Jwk))) continue
    const signer = await jkt(e.by_public_jwk as Jwk)
    let en = endorsers.get(e.by)
    if (!en) {
      en = { by: e.by, email: e.by_email ?? '', jkt: signer, status: 'new', covers: [] }
      if (opts.self && e.by === opts.self.userId) {
        en.status = signer === selfJkt ? 'self' : 'changed'
        if (en.status === 'changed') en.was = selfJkt
      } else if (pins[e.by]) {
        en.status = pins[e.by].jkt === signer ? 'pinned' : 'changed'
        if (en.status === 'changed') en.was = pins[e.by].jkt
      } else if (accept.has(e.by)) {
        en.status = 'accepted'
      }
      endorsers.set(e.by, en)
    } else if (en.jkt !== signer) {
      en.status = 'changed'  // one person, two keys in one answer: refuse
    }
    en.covers.push(keyOf(e.kind, e.target))
  }
  const trusted = new Set<string>()
  for (const en of endorsers.values()) {
    if (en.status === 'self' || en.status === 'pinned' || en.status === 'accepted') en.covers.forEach((c) => trusted.add(c))
  }
  // The writer's own member key needs nobody's word.
  if (opts.self) {
    const mine = real.get(keyOf('member', opts.self.userId))
    if (mine && mine.jkt === selfJkt) trusted.add(keyOf('member', opts.self.userId))
  }
  const sealTo: Recipient[] = []
  for (const [k, { r }] of real) {
    if (trusted.has(k)) sealTo.push({ kind: r.kind, target: r.target, publicJwk: r.public_jwk as Jwk })
    else unconfirmed.push({ ...r, why: 'not confirmed by anyone you trust yet' })
  }
  return { agentId: data.agent_id, kv: data.kv, sealTo, unconfirmed, endorsers: [...endorsers.values()],
           reachesAMachine: sealTo.some((r) => r.kind === 'device') }
}

/** What stops sealing outright: a key that changed under a person you trust. */
export function changedKey(a: Assessment): Endorser | undefined {
  return a.endorsers.find((e) => e.status === 'changed')
}

/** People whose endorsements the writer has not decided about yet. */
export function newEndorsers(a: Assessment): Endorser[] {
  return a.endorsers.filter((e) => e.status === 'new')
}
