import type { ReactNode } from 'react'
import type { AppHost, AppViewResource } from 'canopy-ui/chat'
import type { SealedState } from '@/vault/useSealedConversation'
import { openSealed, type ConversationKey } from '@/vault/sealedChat'

/**
 * What a sensitive conversation needs from the person before it can be read: unlock
 * their vault key, or — when they hold no key to it — why not. Nothing for an
 * ordinary conversation, or one already open.
 */
export function SealedConversationBanner({
  state, error, onUnlock, unlockElement, needsMigration, openElsewhere,
}: {
  state: SealedState
  error: string | null
  onUnlock: () => void
  /** The vault's unlock prompt (passkey / passphrase), rendered where it is asked. */
  unlockElement?: ReactNode
  needsMigration?: boolean
  /** Where else it can be read (the panel links to canopy's own chat page). */
  openElsewhere?: ReactNode
}) {
  if (state === 'plain' || (state === 'open' && !needsMigration)) return null
  return (
    <div data-testid="sealed-banner" className="border-b border-border bg-muted/40 px-4 py-2 text-[12px] text-foreground">
      {(state === 'locked' || state === 'error' || state === 'opening') && (
        <div className="flex flex-wrap items-center gap-2">
          <span>This conversation is sealed to the people in it; your vault key opens it, in this browser.</span>
          <button type="button" onClick={onUnlock} disabled={state === 'opening'} data-testid="sealed-unlock"
                  className="min-h-11 rounded-md bg-primary px-3 py-1 font-medium text-primary-foreground disabled:opacity-50 sm:min-h-0">
            {state === 'opening' ? 'Unlocking…' : 'Unlock to read this conversation'}
          </button>
          {openElsewhere}
        </div>
      )}
      {state === 'no-wrap' && (
        <p className="m-0" data-testid="sealed-no-wrap">
          You have not been given a key to this conversation, so its earlier messages cannot be read here.
          Ask someone in it to add you again from their browser.
        </p>
      )}
      {state === 'stale-wrap' && (
        <p className="m-0" data-testid="sealed-stale-wrap">
          Your vault key has changed since you were given this conversation, so your copy of its key no longer
          opens. Ask someone in it to add you again from their browser.
        </p>
      )}
      {needsMigration && (
        <p className="m-0 mt-1 text-muted-foreground">
          Some earlier messages are still under canopy&apos;s former key and read again once they are moved to this
          conversation&apos;s own key.
        </p>
      )}
      {error && state === 'error' && <p className="m-0 mt-1 text-destructive">{error}</p>}
      {unlockElement}
    </div>
  )
}

/** An MCP Apps host whose Views get a sensitive conversation's call input decrypted
 *  here (`tool_input_sealed`), since the server sends it sealed. Without the key the
 *  View gets what the server sent: no input. */
export function sealedAppHost(host: AppHost, key: () => ConversationKey | null): AppHost {
  return {
    ...host,
    load: async (callId) => {
      const res = (await host.load(callId)) as AppViewResource & {
        tool_input_sealed?: string | null; tool_input_path?: string
      }
      const k = key()
      if (!res.tool_input_sealed || !k) return res
      try {
        const words = await openSealed(res.tool_input_sealed, k)
        const input = (words.content.input ?? {}) as Record<string, unknown>
        const args = res.tool_input_path === 'gateway' ? (input.arguments ?? {}) : input
        return { ...res, tool_input: args as Record<string, unknown> }
      } catch {
        return res
      }
    },
  }
}
