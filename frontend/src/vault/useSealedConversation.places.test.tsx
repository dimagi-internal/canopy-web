// @vitest-environment jsdom
// A private conversation across a reconnect, and rows not yet sealed: what arrived live
// in this tab still reads after a snapshot replaces it with its sealed copy (no unlock
// asked), and a metadata-only row says it is being sealed instead of drawing a blank.
import { renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { wrapInfo, type SealedKey } from './sealedChat'
import { LOCKED_TEXT, SEALING_TEXT, useSealedConversation } from './useSealedConversation'
import { generateKeyPair, wrapEcdhEs } from './zk'

async function sealedKey(): Promise<SealedKey> {
  const person = await generateKeyPair()
  const conv = await generateKeyPair()
  return { conversation: 'conv-1', public_jwk: conv.publicJwk, current: true,
           wrap: await wrapEcdhEs(conv.scalar, person.publicJwk, wrapInfo('conv-1', '7')) }
}

type R = { plaintext: string; content: Record<string, unknown>; sealed?: string; turn_index: number; role: string }
const META = { redacted: 'sensitive', bytes: 10, sha256: 'x' }

describe('useSealedConversation across a reconnect', () => {
  it('keeps reading what arrived live once the snapshot sends it sealed', async () => {
    const key = await sealedKey()
    const live: R[] = [
      { plaintext: 'How is Amina?', content: { text: 'How is Amina?' }, turn_index: 0, role: 'user' },
      { plaintext: '12 visits', content: { text: '12 visits' }, turn_index: 128, role: 'assistant' },
    ]
    const unlock = vi.fn()
    const { result, rerender } = renderHook(({ m }) => useSealedConversation(key, m, unlock), { initialProps: { m: live } })
    expect(result.current.messages.map((m) => m.plaintext)).toEqual(['How is Amina?', '12 visits'])
    // The socket reconnects: the same rows, sealed, plus one this tab never heard.
    const snapshot: R[] = [
      { plaintext: '', content: META, sealed: 'cc1:a', turn_index: 0, role: 'user' },
      { plaintext: '', content: META, sealed: 'cc1:b', turn_index: 128, role: 'assistant' },
      { plaintext: '', content: META, sealed: 'cc1:c', turn_index: 192, role: 'assistant' },
    ]
    rerender({ m: snapshot })
    expect(result.current.messages.map((m) => m.plaintext)).toEqual(['How is Amina?', '12 visits', LOCKED_TEXT])
    expect(result.current.hasSealed).toBe(true)
    rerender({ m: snapshot.slice(0, 2) })
    // Only rows it heard: nothing to unlock.
    expect(result.current.hasSealed).toBe(false)
    expect(unlock).not.toHaveBeenCalled()
  })

  it("reads this tab's own send once its sealed copy lands at a different place", async () => {
    const key = await sealedKey()
    // The optimistic row: the index is the browser's guess.
    const sent: R[] = [{ plaintext: 'How is Amina?', content: { text: 'How is Amina?', client_id: 'c1' },
                         turn_index: 1, role: 'user' }]
    const unlock = vi.fn()
    const { result, rerender } = renderHook(({ m }) => useSealedConversation(key, m, unlock), { initialProps: { m: sent } })
    // The server's copy: sealed, at the transcript's ordinal, keeping the send's nonce.
    rerender({ m: [{ plaintext: '', content: { ...META, client_id: 'c1' }, sealed: 'cc1:a', turn_index: 0, role: 'user' }] })
    expect(result.current.messages.map((m) => m.plaintext)).toEqual(['How is Amina?'])
    expect(result.current.hasSealed).toBe(false)
    expect(unlock).not.toHaveBeenCalled()
  })

  it('a row with only its metadata reads as being sealed, not as a blank bubble', async () => {
    const key = await sealedKey()
    const rows: R[] = [{ plaintext: '', content: META, turn_index: 0, role: 'assistant' },
                       { plaintext: '', content: { ...META, name: 'site_call' }, turn_index: 64, role: 'tool_use' }]
    const { result } = renderHook(() => useSealedConversation(key, rows, vi.fn()))
    expect(result.current.messages[0].plaintext).toBe(SEALING_TEXT)
    // A tool call draws from its metadata, as before.
    expect(result.current.messages[1].plaintext).toBe('')
  })
})
