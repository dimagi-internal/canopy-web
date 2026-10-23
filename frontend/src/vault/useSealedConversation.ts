import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Unlocked } from './operations'
import {
  isSealed,
  openConversationKey,
  openSealed,
  whyNot,
  withWords,
  type ConversationKey,
  type SealedKey,
  type Words,
} from './sealedChat'

/** Where reading a sensitive conversation stands for this person, in this tab. */
export type SealedState =
  | 'plain' // an ordinary conversation: nothing to open
  | 'locked' // sealed; unlock your vault key to read it
  | 'opening'
  | 'open'
  | 'no-wrap' // you were never given a key to it
  | 'stale-wrap' // you were, for a vault key you have since replaced
  | 'error'

/** What a sealed row shows until it is opened. */
export const LOCKED_TEXT = 'Sealed — unlock to read this conversation.'
/** What a row shows when your key does not open it. */
export const UNOPENED_TEXT = 'This message could not be opened with your key.'

type Row = { sealed?: string | null; plaintext: string; content: Record<string, unknown> }

/**
 * A sensitive conversation's rows as this person reads them: the server sends them
 * sealed (`MessageOut.sealed`) with the reader's own wrap (`sealed_key`); once
 * `unlock()` has opened the vault key, every row — the snapshot, scroll-back, and
 * any that arrive later — is decrypted here, in the browser, and nowhere else.
 * Live frames carry the words already and pass through untouched.
 */
export function useSealedConversation<M extends Row>(
  sealedKey: SealedKey | null | undefined,
  messages: M[],
  unlockVault: () => Promise<Unlocked>,
): { state: SealedState; messages: M[]; unlock: () => Promise<void>; error: string | null;
     key: ConversationKey | null; keyRef: { current: ConversationKey | null };
     /** Some row here came sealed — history or scroll-back, not a live frame — so
      *  there is something for this browser to open. */
     hasSealed: boolean } {
  const [key, setKey] = useState<ConversationKey | null>(null)
  const [opening, setOpening] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [, setVersion] = useState(0)
  const opened = useRef(new Map<string, Words | null>())
  const keyRef = useRef<ConversationKey | null>(null)
  keyRef.current = key
  const conversation = sealedKey?.conversation ?? null

  // Another conversation: nothing opened for this one carries over.
  useEffect(() => {
    opened.current = new Map()
    setKey(null)
    setError(null)
  }, [conversation])

  useEffect(() => {
    if (!key) return
    const todo = messages.map((m) => m.sealed).filter((s): s is string => isSealed(s) && !opened.current.has(s))
    if (!todo.length) return
    let live = true
    void Promise.all(todo.map(async (s) => {
      try {
        opened.current.set(s, await openSealed(s, key))
      } catch {
        opened.current.set(s, null)
      }
    })).then(() => live && setVersion((v) => v + 1))
    return () => {
      live = false
    }
  }, [key, messages])

  const unlock = useCallback(async () => {
    if (!sealedKey || opening) return
    setOpening(true)
    setError(null)
    try {
      const me = await unlockVault()
      setKey(await openConversationKey(sealedKey, me))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not unlock this conversation.')
    } finally {
      setOpening(false)
    }
  }, [sealedKey, opening, unlockVault])

  const shown = useMemo(() => !messages.some((m) => isSealed(m.sealed)) ? messages : messages.map((m) => {
    if (!isSealed(m.sealed)) return m
    if (!opened.current.has(m.sealed)) return { ...m, plaintext: m.plaintext || LOCKED_TEXT }
    const words = opened.current.get(m.sealed)
    return words ? withWords(m, words) : { ...m, plaintext: UNOPENED_TEXT }
    // `opened` is a ref; the version bump above is what re-runs this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [messages, key, opened.current.size])

  const state: SealedState = !sealedKey ? 'plain'
    : key ? 'open'
      : opening ? 'opening'
        : whyNot(sealedKey) ?? (error ? 'error' : 'locked')
  const hasSealed = messages.some((m) => isSealed(m.sealed))
  return { state, messages: shown, unlock, error, key, keyRef, hasSealed }
}
