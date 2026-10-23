/**
 * Your private conversations, carried over when you replace your vault key.
 *
 * Each private (sensitive) conversation's key exists only wrapped to its people's
 * vault keys (`sealedChat.ts`). A new vault key opens none of the old wraps, so
 * before the key is replaced this browser — with the OLD key unlocked — opens each
 * copy you hold (`GET /my-keys`), and once the new key is in place wraps each to it
 * (`PUT /my-keys`). canopy-web cannot do this for you: it holds no key that opens them.
 *
 * A copy that does not open with the old key (already stale) cannot be carried; someone
 * else in that conversation can give you a fresh one (People → Give key).
 */
import { myKeyCopies, putMyKeyCopies, type MyKeyCopy } from '@/api/chat'
import type { VaultKeysetOut } from '@/api/vault'
import type { Unlocked } from './operations'
import { wrapInfo } from './sealedChat'
import { rememberSelf } from './trust'
import { jkt, unwrapEcdhEs, wrapEcdhEs, type EcdhWrap, type PublicJwk } from './zk'

type Jwk = { x?: string; y?: string }

/** How a person reads one of their private conversations in a sentence. */
export function nameOf(c: Pick<MyKeyCopy, 'agent_name' | 'started_at'>): string {
  const when = c.started_at ? new Date(c.started_at).toLocaleDateString() : ''
  return `with ${c.agent_name || 'an agent'}${when ? `, started ${when}` : ''}`
}

/** What the confirm before replacing your key says is at stake. */
export function atStake(copies: MyKeyCopy[]): string {
  if (!copies.length) return ''
  const shown = copies.slice(0, 5).map((c) => `• a private conversation ${nameOf(c)}`).join('\n')
  const more = copies.length > 5 ? `\n• and ${copies.length - 5} more` : ''
  return `\n\nYou are in ${copies.length} private conversation${copies.length === 1 ? '' : 's'}, sealed to your `
    + `current key:\n${shown}${more}\n\nThis browser will unlock your current key first and carry them over to the `
    + 'new one, so you can still read them.'
}

/** The conversation keys you hold, opened with your current (old) vault key. Copies it
 *  does not open are returned as `lost`. */
export async function openMyCopies(copies: MyKeyCopy[], old: Unlocked):
    Promise<{ opened: { copy: MyKeyCopy; scalar: Uint8Array }[]; lost: MyKeyCopy[] }> {
  const opened: { copy: MyKeyCopy; scalar: Uint8Array }[] = []
  const lost: MyKeyCopy[] = []
  for (const copy of copies) {
    try {
      const scalar = await unwrapEcdhEs(copy.wrap as unknown as EcdhWrap, old.privateKey, old.publicJwk,
        wrapInfo(copy.conversation, old.userId))
      opened.push({ copy, scalar })
    } catch {
      lost.push(copy)
    }
  }
  return { opened, lost }
}

/** Wrap each opened conversation key to the NEW vault key and store them, retrying a
 *  failed store; the opened keys are wiped from memory afterwards either way. */
export async function storeOnNewKey(opened: { copy: MyKeyCopy; scalar: Uint8Array }[], userId: string,
                                    newPublic: Jwk, opts: { retries?: number; pause?: (ms: number) => Promise<void> } = {}):
    Promise<number> {
  if (!opened.length) return 0
  const thumb = await jkt(newPublic)
  try {
    const copies = await Promise.all(opened.map(async ({ copy, scalar }) => ({
      session_id: copy.session_id,
      wrap: await wrapEcdhEs(scalar, newPublic as PublicJwk, wrapInfo(copy.conversation, userId)),
      recipient_jkt: thumb,
    })))
    const retries = opts.retries ?? 3
    const pause = opts.pause ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)))
    for (let attempt = 0; ; attempt++) {
      try {
        return (await putMyKeyCopies(copies)).stored
      } catch (e) {
        if (attempt >= retries) throw e
        await pause(500 * 2 ** attempt)
      }
    }
  } finally {
    for (const o of opened) o.scalar.fill(0)
  }
}

/**
 * Replace your vault key, keeping your private conversations: list your copies, unlock
 * the OLD key (`unlockOld`), open them, make the new key (`makeNew`), then store each
 * copy wrapped to it. Returns the new keyset and what could not be carried.
 */
export async function replaceKeepingConversations(
  unlockOld: () => Promise<Unlocked>,
  makeNew: () => Promise<{ keyset: VaultKeysetOut; me: Unlocked }>,
  copies?: MyKeyCopy[],
): Promise<{ keyset: VaultKeysetOut; carried: number; lost: MyKeyCopy[] }> {
  const held = copies ?? await myKeyCopies()
  let opened: { copy: MyKeyCopy; scalar: Uint8Array }[] = []
  let lost: MyKeyCopy[] = []
  if (held.length) ({ opened, lost } = await openMyCopies(held, await unlockOld()))
  let made: { keyset: VaultKeysetOut; me: Unlocked }
  try {
    made = await makeNew()
  } catch (e) {
    for (const o of opened) o.scalar.fill(0)
    throw e
  }
  await rememberSelf(made.me.userId, made.me.publicJwk, made.me.scalar).catch(() => undefined)
  let carried = 0
  try {
    carried = await storeOnNewKey(opened, made.me.userId, made.me.publicJwk)
  } catch (e) {
    throw new Error('Your new vault key is set, but your private conversations could not be carried over to it '
      + `(${e instanceof Error ? e.message : 'the request failed'}). Ask someone in each to give you a fresh copy `
      + '(People → Give key).')
  }
  return { keyset: made.keyset, carried, lost }
}
