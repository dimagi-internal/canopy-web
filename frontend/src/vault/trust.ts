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
import { jkt, publicJwkOfScalar, verifyEndorsement, type Recipient } from './zk'

type Jwk = { x?: string; y?: string }

export interface Pin {
  jkt: string
  email: string
  pinned_at: string
  /** Your OWN key, taken on the server's word when this browser started a chat before it
   *  had ever opened your vault key (trust on first use, `trustSelfOnFirstUse`): not yet
   *  checked against the key you hold. The first unlock here checks it (`rememberSelf`). */
  unconfirmed?: boolean
  /** The chats whose keys were wrapped to it meanwhile, named if that check fails. */
  conversations?: string[]
}
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

// ---- a person's vault key, one at a time (a private conversation) ----------------------
//
// Adding someone to a private conversation wraps its key to THEIR vault key, which
// the server names (`GET …/participants/recipient`). The same rule as above, for one
// person and no endorsements: wrap only to a key this browser already trusts for them
// (pinned), or one the adder accepts NOW having seen its fingerprint (first contact,
// then pinned). A pinned person whose key changed stops the add until the adder has
// confirmed the new key with them.

export type PersonStatus = 'self' | 'pinned' | 'new' | 'changed' | 'bad'

export interface PersonVerdict {
  status: PersonStatus
  /** The key's thumbprint, recomputed here. */
  jkt: string
  /** For `changed`: the thumbprint pinned before. */
  was?: string
}

/** How far this browser trusts `recipient`'s vault key. `bad`: the key does not match the
 *  thumbprint the server sent with it. */
export async function trustPerson(recipient: { user_id: number | string; public_jwk: Jwk | Record<string, unknown>; jkt: string },
                                  opts: { self?: Self | null; pins?: Pins } = {}): Promise<PersonVerdict> {
  const real = await jkt(recipient.public_jwk as Jwk)
  if (real !== recipient.jkt) return { status: 'bad', jkt: real }
  const by = String(recipient.user_id)
  if (opts.self && opts.self.userId === by) {
    const mine = await jkt(opts.self.publicJwk)
    return mine === real ? { status: 'self', jkt: real } : { status: 'changed', jkt: real, was: mine }
  }
  const pin = (opts.pins ?? loadPins())[by]
  if (!pin) return { status: 'new', jkt: real }
  return pin.jkt === real ? { status: 'pinned', jkt: real } : { status: 'changed', jkt: real, was: pin.jkt }
}

/** Remember the person's key as trusted in this browser (they accepted its fingerprint). */
export function pinPerson(userId: number | string, thumb: string, email: string): void {
  savePins([{ by: String(userId), jkt: thumb, email }])
}

/** Your own key, as you just opened it with your passkey or passphrase: pinned, so a key
 *  the server later names as yours is checked against it (`checkSelf`). The public half
 *  is recomputed from the private scalar when the flow opened it, so the pin is the key
 *  you hold, whatever the server said it was. */
export async function rememberSelf(userId: string, publicJwk: Jwk, scalar?: Uint8Array): Promise<SelfMismatch | null> {
  const thumb = await jkt(scalar ? await publicJwkOfScalar(scalar) : publicJwk)
  const was = loadPins()[userId]
  // A key taken on first use, now checked against the key actually held: the chats
  // started meanwhile were wrapped to a key that is NOT yours — what a substituted key
  // looks like. Recorded, to be shown with their names (`selfMismatch`); the pin becomes
  // the key you hold, so the substituted one is refused from now on (`checkSelf`).
  let mismatch: SelfMismatch | null = null
  if (was?.unconfirmed && was.jkt !== thumb && scalar) {
    mismatch = { userId, wrongJkt: was.jkt, conversations: was.conversations ?? [], found_at: new Date().toISOString() }
    writeMismatch(mismatch)
  }
  if (!was || was.jkt !== thumb || (was.unconfirmed && scalar)) {
    // Only a key opened from its private half confirms; a public half alone keeps the doubt.
    writePin(userId, { jkt: thumb, email: was?.email ?? '', pinned_at: new Date().toISOString(),
                       ...(scalar ? {} : { unconfirmed: true, conversations: was?.conversations ?? [] }) })
  }
  return mismatch
}

/** Starting a chat in a browser that has never opened your vault key: its key is wrapped
 *  to the key canopy names as yours WITHOUT asking you to unlock (starting a chat never
 *  does), and that key is pinned here as UNCONFIRMED, with the chat's id. Your first
 *  unlock here checks it (`rememberSelf`). A pin already there (confirmed or not, the
 *  same key) just gains the chat. Storage this browser cannot keep keeps nothing: each
 *  start trusts anew, and nothing is checked later. */
export function trustSelfOnFirstUse(userId: string, thumb: string, conversation: string): void {
  const was = loadPins()[userId]
  if (was && was.jkt !== thumb) return  // `checkSelf` said changed: the caller refused
  if (was && !was.unconfirmed) return
  writePin(userId, { jkt: thumb, email: was?.email ?? '', pinned_at: was?.pinned_at ?? new Date().toISOString(),
                     unconfirmed: true, conversations: [...(was?.conversations ?? []), conversation].slice(-200) })
}

/** Your own key here was taken on first use and not checked yet. */
export function isUnconfirmed(userId: string): boolean {
  return loadPins()[userId]?.unconfirmed === true
}

function writePin(userId: string, pin: Pin): void {
  try {
    const pins = loadPins()
    pins[userId] = pin
    window.localStorage.setItem(PINS, JSON.stringify(pins))
  } catch {
    // storage blocked: nothing is remembered here
  }
}

/** Your vault key, opened here, is not the one chats started in this browser before were
 *  wrapped to (`rememberSelf`). Shown until dismissed. */
export interface SelfMismatch { userId: string; wrongJkt: string; conversations: string[]; found_at: string }

const MISMATCH = 'canopy-vault/v1/self-mismatch'

function writeMismatch(m: SelfMismatch): void {
  try {
    window.localStorage.setItem(MISMATCH, JSON.stringify(m))
  } catch {
    // storage blocked: the caller still has it to show now
  }
}

export function selfMismatch(userId: string): SelfMismatch | null {
  try {
    const raw = window.localStorage.getItem(MISMATCH)
    const m = raw ? (JSON.parse(raw) as SelfMismatch) : null
    return m && m.userId === userId ? m : null
  } catch {
    return null
  }
}

export function dismissSelfMismatch(): void {
  try {
    window.localStorage.removeItem(MISMATCH)
  } catch {
    // nothing to clear
  }
}

/** The sentence a mismatch is shown in, naming the chats it touches. */
export function selfMismatchMessage(m: SelfMismatch): string {
  const n = m.conversations.length
  const which = n ? ` ${n === 1 ? 'This chat was' : `These ${n} chats were`}: ${m.conversations.join(', ')}.` : ''
  return 'Warning: chats started in this browser before you first opened your vault key here were ' +
    'locked to a key that is not yours, so whoever holds that key could read them if they turn private, ' +
    `and you cannot.${which} This browser no longer uses that key. If you did not replace your vault key ` +
    'around then, someone may be trying to read your chats: tell your canopy admin.'
}

/** Whether `thumb` is the vault key this browser last opened as yours (or took on first
 *  use, not yet checked): `new` when it has none, `changed` when it holds a different one. */
export function checkSelf(userId: string, thumb: string): 'pinned' | 'new' | 'changed' {
  const pin = loadPins()[userId]
  if (!pin) return 'new'
  return pin.jkt === thumb ? 'pinned' : 'changed'
}
