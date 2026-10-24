import type { ReactNode } from 'react'
import type { AppHost, AppViewResource } from 'canopy-ui/chat'
import type { SealedState } from '@/vault/useSealedConversation'
import { openSealed, type ConversationKey } from '@/vault/sealedChat'

/**
 * What a sensitive conversation needs from the person before its stored rows can be
 * read: unlock their vault key, or — when they hold no key to it — why not. Shown
 * only when there IS something sealed this browser has not opened (`hasSealed`:
 * history after a reload, scroll-back); a conversation whose messages all arrived
 * live here needs nothing. The unlock prompt's fallback (a passphrase, when the
 * passkey is unavailable or failed) and an unlock error render whenever present.
 */
export function SealedConversationBanner({
  state, error, onUnlock, unlockElement, needsMigration, openElsewhere, hasSealed = true, method = 'passkey',
  onSetAside,
}: {
  state: SealedState
  error: string | null
  onUnlock: () => void
  /** The vault's unlock prompt (passphrase fallback), rendered where it is asked. */
  unlockElement?: ReactNode
  needsMigration?: boolean
  /** Where else it can be read, for when unlocking cannot work here (a frame that
   *  may not use passkeys); the caller passes it only then. */
  openElsewhere?: ReactNode
  /** Some row came sealed and is not open yet. */
  hasSealed?: boolean
  /** How the person's vault key opens: says "passkey" or "passphrase". */
  method?: 'passkey' | 'passphrase'
  /** Their vault key opened and their copy of the conversation key did not: set it
   *  aside (`DELETE …/key-copies/mine`) so someone in it can give them a fresh one. */
  onSetAside?: () => void
}) {
  const lockedish = state === 'locked' || state === 'error' || state === 'opening'
  const showLocked = hasSealed && lockedish
  const showNoWrap = hasSealed && state === 'no-wrap'
  const showStale = hasSealed && state === 'stale-wrap'
  const showMigration = Boolean(needsMigration) && state === 'open'
  const showError = Boolean(error) && state === 'error'
  if (!showLocked && !showNoWrap && !showStale && !showMigration && !unlockElement && !showError) return null
  return (
    <div data-testid="sealed-banner" className="border-b border-border bg-muted/40 px-4 py-2 text-[12px] text-foreground">
      {showLocked && (
        <div className="flex flex-wrap items-center gap-2">
          <span>
            Earlier messages are locked. Unlock with your {method} to read them.
          </span>
          <button type="button" onClick={onUnlock} disabled={state === 'opening'} data-testid="sealed-unlock"
                  className="min-h-11 rounded-md bg-primary px-3 py-1 font-medium text-primary-foreground disabled:opacity-50 sm:min-h-0">
            {state === 'opening' ? 'Unlocking…' : 'Unlock'}
          </button>
          {openElsewhere}
        </div>
      )}
      {showNoWrap && (
        <p className="m-0" data-testid="sealed-no-wrap">
          You have not been given a key to this conversation, so its earlier messages cannot be read here.
          Ask someone in it to add you again from their browser.
        </p>
      )}
      {showStale && (
        <p className="m-0" data-testid="sealed-stale-wrap">
          Your vault key has changed since you were given this conversation, so your copy of its key no longer
          opens. Ask someone in it to add you again from their browser.
        </p>
      )}
      {showMigration && (
        <p className="m-0 mt-1 text-muted-foreground">
          Some earlier messages are still under canopy&apos;s former key and read again once they are moved to this
          conversation&apos;s own key.
        </p>
      )}
      {showError && (
        <p className="m-0 mt-1 text-destructive">
          {error} {!showLocked && openElsewhere}
        </p>
      )}
      {showError && onSetAside && (
        <div className="mt-1 flex flex-wrap items-center gap-2" data-testid="sealed-copy-broken">
          <span>Your copy of this conversation&apos;s key does not open. Set it aside, and someone in it can give you a fresh one.</span>
          <button type="button" onClick={onSetAside} data-testid="sealed-set-aside"
                  className="rounded-md border border-input px-3 py-1 text-foreground">
            Set my copy aside
          </button>
        </div>
      )}
      {unlockElement}
    </div>
  )
}

/** An MCP Apps host whose Views get a sensitive conversation's call input decrypted
 *  here (`tool_input_sealed`), since the server sends it sealed. Without the key the
 *  View gets what the server sent: no input — and `opened` (whether the key is open
 *  now) is the host's `inputRevision`, so every View loads again once it is. */
export function sealedAppHost(host: AppHost, key: () => ConversationKey | null, opened = false): AppHost {
  return {
    ...host,
    inputRevision: opened ? 'open' : 'locked',
    load: async (callId) => {
      const res = (await host.load(callId)) as AppViewResource & {
        tool_input_sealed?: string | null; tool_input_path?: string
      }
      const k = key()
      if (!res.tool_input_sealed || !k) return res
      try {
        const words = await openSealed(res.tool_input_sealed, k, { role: 'tool_use' })
        const input = (words.content.input ?? {}) as Record<string, unknown>
        const args = res.tool_input_path === 'gateway' ? (input.arguments ?? {}) : input
        return { ...res, tool_input: args as Record<string, unknown> }
      } catch {
        return res
      }
    },
  }
}
