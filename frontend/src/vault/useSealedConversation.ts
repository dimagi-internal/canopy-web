import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { dismissSelfMismatch, selfMismatch, selfMismatchMessage } from './trust'
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
/** What a row of a private conversation shows while its words are not sealed to the
 *  conversation's key yet (kept under canopy's earlier key, or written before it had
 *  one): something is there, and is on its way to being readable. */
export const SEALING_TEXT = 'Sealing…'

type Row = { sealed?: string | null; plaintext: string; content: Record<string, unknown>;
             turn_index?: number; role?: string }

const placeOf = (m: Row) => `${m.turn_index ?? ''}|${m.role ?? ''}`
const openedKey = (m: Row & { sealed: string }) => `${placeOf(m)}|${m.sealed}`
const isProse = (m: Row) => m.role === 'user' || m.role === 'assistant'
const isMetadataOnly = (m: Row) => (m.content as { redacted?: unknown } | undefined)?.redacted === "sensitive"

/**
 * A sensitive conversation's rows as this person reads them: the server sends them
 * sealed (`MessageOut.sealed`) with the reader's own wrap (`sealed_key`); once
 * `unlock()` has opened the vault key, every row — the snapshot, scroll-back, and
 * any that arrive later — is decrypted here, in the browser, and nowhere else, and
 * only where its envelope says it belongs (its index and speaker).
 * Live frames carry the words already and pass through untouched — and are kept for
 * the life of the tab, by place, so a reconnect's snapshot (which carries the same
 * rows sealed) reads them still, without asking to unlock.
 */
export function useSealedConversation<M extends Row>(
  sealedKey: SealedKey | null | undefined,
  messages: M[],
  unlockVault: () => Promise<Unlocked>,
): { state: SealedState; messages: M[]; unlock: () => Promise<void>; error: string | null;
     key: ConversationKey | null; keyRef: { current: ConversationKey | null };
     /** Some row here came sealed — history or scroll-back, not a live frame — so
      *  there is something for this browser to open. */
     hasSealed: boolean
     /** The vault key opened, and this person's copy of the conversation key did not:
      *  it is broken, and only they can set it aside for a fresh one. */
     copyBroken: boolean
     /** The vault key opened here is not the one chats started in this browser before
      *  were wrapped to (trust on first use, `trust.rememberSelf`): the sentence naming
      *  them, until dismissed. */
     warning: string | null; dismissWarning: () => void } {
  const [key, setKey] = useState<ConversationKey | null>(null)
  const [copyBroken, setCopyBroken] = useState(false)
  const [opening, setOpening] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [warning, setWarning] = useState<string | null>(null)
  const [, setVersion] = useState(0)
  const opened = useRef(new Map<string, Words | null>())
  // The words of rows that arrived live in this tab, by place (turn index + role).
  const heard = useRef(new Map<string, Words>())
  const keyRef = useRef<ConversationKey | null>(null)
  keyRef.current = key
  const conversation = sealedKey?.conversation ?? null

  // Another conversation: nothing opened for this one carries over.
  useEffect(() => {
    opened.current = new Map()
    setKey(null)
    setError(null)
  }, [conversation])
  // What was heard is reset as the conversation changes, in the render itself: an effect
  // would run after this render has already recorded the new conversation's rows.
  const heardFor = useRef(conversation)
  if (heardFor.current !== conversation) {
    heardFor.current = conversation
    heard.current = new Map()
  }

  // Remember what arrived live, before a snapshot replaces it with its sealed copy.
  if (sealedKey) {
    for (const m of messages) {
      if (isSealed(m.sealed) || m.turn_index === undefined || isMetadataOnly(m)) continue
      if (m.plaintext || (m.content && Object.keys(m.content).length)) {
        heard.current.set(placeOf(m), { plaintext: m.plaintext, content: m.content })
      }
    }
  }

  useEffect(() => {
    if (!key) return
    const todo = messages.filter((m): m is M & { sealed: string } =>
      isSealed(m.sealed) && !opened.current.has(openedKey(m as M & { sealed: string })))
    if (!todo.length) return
    let live = true
    void Promise.all(todo.map(async (m) => {
      try {
        opened.current.set(openedKey(m), await openSealed(m.sealed, key, { index: m.turn_index, role: m.role }))
      } catch {
        opened.current.set(openedKey(m), null)
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
    setCopyBroken(false)
    try {
      const me = await unlockVault()
      // The first unlock in this browser checks the key chats started here were wrapped
      // to before it had ever opened yours (`trust.trustSelfOnFirstUse`).
      const mismatch = selfMismatch(me.userId)
      setWarning(mismatch ? selfMismatchMessage(mismatch) : null)
      try {
        setKey(await openConversationKey(sealedKey, me))
      } catch (e) {
        setCopyBroken(true)
        throw e
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not unlock this conversation.')
    } finally {
      setOpening(false)
    }
  }, [sealedKey, opening, unlockVault])

  const shown = useMemo(() => {
    if (!sealedKey) return messages
    let changed = false
    const out = messages.map((m) => {
      const next = shownRow(m)
      if (next !== m) changed = true
      return next
    })
    return changed ? out : messages
    function shownRow(m: M): M {
      if (!isSealed(m.sealed)) {
        // A private conversation's row with only its metadata: sealed words still to come.
        return isProse(m) && !m.plaintext && isMetadataOnly(m) ? { ...m, plaintext: SEALING_TEXT } : m
      }
      const k = openedKey(m as M & { sealed: string })
      if (!opened.current.has(k)) {
        const live = heard.current.get(placeOf(m))
        return live ? withWords(m, live) : { ...m, plaintext: m.plaintext || LOCKED_TEXT }
      }
      const words = opened.current.get(k)
      return words ? withWords(m, words) : { ...m, plaintext: UNOPENED_TEXT }
    }
    // `opened` is a ref; the version bump above is what re-runs this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, key, sealedKey, opened.current.size])

  const state: SealedState = !sealedKey ? 'plain'
    : key ? 'open'
      : opening ? 'opening'
        : whyNot(sealedKey) ?? (error ? 'error' : 'locked')
  // Something sealed this browser cannot show yet: neither opened nor heard live here.
  const hasSealed = messages.some((m) => isSealed(m.sealed) && !heard.current.has(placeOf(m)))
  const dismissWarning = useCallback(() => {
    dismissSelfMismatch()
    setWarning(null)
  }, [])
  return { state, messages: shown, unlock, error, key, keyRef, hasSealed, copyBroken, warning, dismissWarning }
}
