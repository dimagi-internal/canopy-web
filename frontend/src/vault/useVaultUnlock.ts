import type { ReactNode } from 'react'
import { getKeyset } from '@/api/vault'
import { stepUpAndUnlock, unlockVaultKey, type Unlocked, type UnlockPrompt } from './operations'
import { rememberSelf } from './trust'
import { useUnlockPrompt } from './UnlockPrompt'

const hasSeal = (ks: { seals?: unknown }, alg: string) =>
  ((ks.seals ?? []) as { alg?: string }[]).some((s) => s.alg === alg)

/**
 * My vault key, unlocked, for a flow that needs it: one passkey touch both steps up
 * and opens it — straight from the click, no second "use my passkey" step; a
 * passphrase only when there is no passkey seal, or the passkey failed (render
 * `element` for that prompt). The one way a page asks — a runner's credentials, a
 * vault's own page, a sensitive conversation.
 */
export function useVaultUnlock(): { unlock: () => Promise<Unlocked>; element: ReactNode } {
  const unlocker = useUnlockPrompt()
  const unlock = async (): Promise<Unlocked> => remembered(await open())
  const open = async (): Promise<Unlocked> => {
    const ks = await getKeyset()
    let failed: unknown = null
    try {
      const me = await stepUpAndUnlock(ks)
      if (me) return me
    } catch (e) {
      failed = e
    }
    // No passkey seal: the person chooses (a passphrase). A passkey tried that did not
    // open it: offer the passphrase, never the passkey a second time.
    if (!failed && !hasSeal(ks, 'prf')) return unlockVaultKey(unlocker.prompt, ks)
    if (!hasSeal(ks, '2skd')) throw failed ?? new Error('Your passkey did not open your vault key.')
    const passphraseOnly: UnlockPrompt = (ask) => unlocker.prompt({ ...ask, passkey: false })
    return unlockVaultKey(passphraseOnly, ks)
  }
  return { unlock, element: unlocker.element }
}

/** The key just opened is this person's own: pinned, so a key the server later names as
 *  theirs is checked against it (`trust.checkSelf`). */
async function remembered(me: Unlocked): Promise<Unlocked> {
  await rememberSelf(me.userId, me.publicJwk, me.scalar).catch(() => undefined)
  return me
}
