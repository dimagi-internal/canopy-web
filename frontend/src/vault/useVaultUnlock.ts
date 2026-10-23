import type { ReactNode } from 'react'
import { getKeyset } from '@/api/vault'
import { stepUpAndUnlock, unlockVaultKey, type Unlocked } from './operations'
import { useUnlockPrompt } from './UnlockPrompt'

/**
 * My vault key, unlocked, for a flow that needs it: one passkey touch both steps up
 * and opens it; a passphrase otherwise (render `element` for its prompt). The one
 * way a page asks — a runner's credentials, a vault's own page.
 */
export function useVaultUnlock(): { unlock: () => Promise<Unlocked>; element: ReactNode } {
  const unlocker = useUnlockPrompt()
  const unlock = async (): Promise<Unlocked> => {
    const ks = await getKeyset()
    return (await stepUpAndUnlock(ks).catch(() => null)) ?? unlockVaultKey(unlocker.prompt, ks)
  }
  return { unlock, element: unlocker.element }
}
