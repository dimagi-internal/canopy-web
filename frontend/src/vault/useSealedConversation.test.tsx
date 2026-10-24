// @vitest-environment jsdom
// Reading a sensitive conversation in the chat page and the panel: rows arrive sealed
// with the reader's wrap, stay sealed until the vault is unlocked, and open here.
// The REAL crypto runs; a conversation is made the way the server makes one.
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { SealedConversationBanner, sealedAppHost } from '@/components/chat/SealedConversationBanner'
import type { AppHost } from 'canopy-ui/chat'
import { SEALED_PREFIX, messageInfo, openConversationKey, wrapInfo, type SealedKey } from './sealedChat'
import { LOCKED_TEXT, UNOPENED_TEXT, useSealedConversation } from './useSealedConversation'
import { b64u, generateKeyPair, wrapEcdhEs } from './zk'

const enc = new TextEncoder()

async function conversation(userId = '7') {
  const person = await generateKeyPair()
  const conv = await generateKeyPair()
  const sealedKey: SealedKey = {
    conversation: 'conv-1', public_jwk: conv.publicJwk, current: true,
    wrap: await wrapEcdhEs(conv.scalar, person.publicJwk, wrapInfo('conv-1', userId)),
  }
  const seal = async (i: number, words: object) => {
    const env = await wrapEcdhEs(enc.encode(JSON.stringify(words)), conv.publicJwk, messageInfo('conv-1', i))
    return SEALED_PREFIX + b64u(enc.encode(JSON.stringify({ c: 'conv-1', i, ...env })))
  }
  const me = { userId, privateKey: person.privateKey, publicJwk: person.publicJwk }
  return { sealedKey, seal, me }
}

describe('useSealedConversation', () => {
  it('leaves an ordinary conversation exactly as it came', () => {
    const rows = [{ plaintext: 'hi', content: {} }]
    const { result } = renderHook(() => useSealedConversation(null, rows, vi.fn()))
    expect(result.current.state).toBe('plain')
    expect(result.current.messages).toBe(rows)
  })

  it('shows sealed rows as sealed until unlocked, then their words — older rows and new ones', async () => {
    const c = await conversation()
    type R = { plaintext: string; content: Record<string, unknown>; sealed?: string }
    const rows: R[] = [
      { plaintext: '', content: { redacted: 'sensitive', client_id: 'k' }, sealed: await c.seal(0, { plaintext: 'How is Amina?', content: { text: 'How is Amina?' } }) },
      { plaintext: 'live words', content: {} },
    ]
    const unlockVault = vi.fn(async () => c.me)
    const { result, rerender } = renderHook(({ m }) => useSealedConversation(c.sealedKey, m, unlockVault),
                                            { initialProps: { m: rows } })
    expect(result.current.state).toBe('locked')
    expect(result.current.messages[0].plaintext).toBe(LOCKED_TEXT)
    expect(result.current.messages[1].plaintext).toBe('live words')
    await act(() => result.current.unlock())
    await waitFor(() => expect(result.current.messages[0].plaintext).toBe('How is Amina?'))
    expect(result.current.state).toBe('open')
    expect(result.current.messages[0].content).toEqual({ text: 'How is Amina?', client_id: 'k' })
    expect(unlockVault).toHaveBeenCalledTimes(1)
    // Scroll-back arrives later: opened with the key already in hand.
    const older = { plaintext: '', content: {}, sealed: await c.seal(1, { plaintext: '12 visits', content: {} }) }
    rerender({ m: [older, ...rows] })
    await waitFor(() => expect(result.current.messages[0].plaintext).toBe('12 visits'))
  })

  it('says so when a row does not open with this key', async () => {
    const c = await conversation()
    const other = await conversation()
    const rows = [{ plaintext: '', content: {}, sealed: await other.seal(0, { plaintext: 'x', content: {} }) }]
    const { result } = renderHook(() => useSealedConversation(c.sealedKey, rows, async () => c.me))
    await act(() => result.current.unlock())
    await waitFor(() => expect(result.current.messages[0].plaintext).toBe(UNOPENED_TEXT))
  })

  it('reports no wrap, a stale wrap, and a failed unlock without opening anything', async () => {
    const c = await conversation()
    const none = renderHook(() => useSealedConversation({ ...c.sealedKey, wrap: null }, [], vi.fn()))
    expect(none.result.current.state).toBe('no-wrap')
    const stale = renderHook(() => useSealedConversation({ ...c.sealedKey, current: false }, [], vi.fn()))
    expect(stale.result.current.state).toBe('stale-wrap')
    const failing = renderHook(() => useSealedConversation(c.sealedKey, [],
      async () => { throw new Error('Unlock cancelled — nothing was changed.') }))
    await act(() => failing.result.current.unlock())
    expect(failing.result.current.state).toBe('error')
    expect(failing.result.current.error).toMatch(/cancelled/)
  })
})

describe('SealedConversationBanner', () => {
  it('says nothing on a fresh conversation whose messages all arrived live here', () => {
    render(<SealedConversationBanner state="locked" error={null} onUnlock={vi.fn()} hasSealed={false} />)
    expect(screen.queryByTestId('sealed-banner')).toBeNull()
    render(<SealedConversationBanner state="no-wrap" error={null} onUnlock={vi.fn()} hasSealed={false} />)
    expect(screen.queryByTestId('sealed-banner')).toBeNull()
  })

  it('offers one plain unlock when sealed rows are waiting, and is gone once open', () => {
    const onUnlock = vi.fn()
    const { rerender } = render(<SealedConversationBanner state="locked" error={null} onUnlock={onUnlock} hasSealed />)
    expect(screen.getByTestId('sealed-banner').textContent)
      .toContain('Earlier messages are locked. Unlock with your passkey to read them.')
    fireEvent.click(screen.getByRole('button', { name: 'Unlock' }))
    expect(onUnlock).toHaveBeenCalled()
    // "Read it in canopy" only when the caller says unlocking cannot work here.
    expect(screen.queryByText('Read it in canopy')).toBeNull()
    rerender(<SealedConversationBanner state="error" error="No passkey was used" onUnlock={onUnlock} hasSealed
                                       openElsewhere={<a href="/x">Read it in canopy</a>} />)
    expect(screen.getByText('Read it in canopy')).toBeTruthy()
    rerender(<SealedConversationBanner state="no-wrap" error={null} onUnlock={onUnlock} hasSealed />)
    expect(screen.getByTestId('sealed-no-wrap').textContent).toMatch(/not been given a key/)
    rerender(<SealedConversationBanner state="open" error={null} onUnlock={onUnlock} hasSealed />)
    expect(screen.queryByTestId('sealed-banner')).toBeNull()
  })

  it("shows the passphrase fallback wherever it is asked, even with nothing sealed in view", () => {
    render(<SealedConversationBanner state="opening" error={null} onUnlock={vi.fn()} hasSealed={false}
                                     unlockElement={<form data-testid="vault-unlock" />} />)
    expect(screen.getByTestId('vault-unlock')).toBeTruthy()
  })
})

describe('useSealedConversation.hasSealed', () => {
  it('is false for live rows and true once a sealed row is in view', async () => {
    const c = await conversation()
    const live = renderHook(() => useSealedConversation(c.sealedKey, [{ plaintext: 'hi', content: {} }], vi.fn()))
    expect(live.result.current.hasSealed).toBe(false)
    const rows = [{ plaintext: '', content: {}, sealed: await c.seal(0, { plaintext: 'x', content: {} }) }]
    const stored = renderHook(() => useSealedConversation(c.sealedKey, rows, vi.fn()))
    expect(stored.result.current.hasSealed).toBe(true)
  })
})

describe('sealedAppHost', () => {
  it("hands a View the call's input, decrypted here, from where its path says", async () => {
    const c = await conversation()
    const key = await openConversationKey(c.sealedKey, c.me)
    const call = { content: { id: 'tu1', name: 'site_call', input: { tool: 'data_query', arguments: { q: 'Amina' } } } }
    const host = { load: vi.fn(async () => ({ tool_input: {}, tool_input_path: 'gateway',
                                              tool_input_sealed: await c.seal(64, call) })) } as unknown as AppHost
    expect((await sealedAppHost(host, () => key).load('tu1')).tool_input).toEqual({ q: 'Amina' })
    // Locked: the View gets what the server sent, which is nothing.
    expect((await sealedAppHost(host, () => null).load('tu1')).tool_input).toEqual({})
  })
})

describe('the first unlock in a browser checks the key earlier chats were wrapped to', () => {
  it('names the chats when the key held is not the one they were wrapped to, until dismissed', async () => {
    const store = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    })
    try {
      const { trustSelfOnFirstUse, rememberSelf } = await import('./trust')
      const { jkt } = await import('./zk')
      const c = await conversation()
      const substituted = await generateKeyPair()
      trustSelfOnFirstUse('7', await jkt(substituted.publicJwk), 'chat-earlier')
      const held = await generateKeyPair()
      // The unlock opens the key actually held, and pins it (`unlockLocally`, `useVaultUnlock`).
      const unlockVault = vi.fn(async () => {
        await rememberSelf('7', held.publicJwk, held.scalar)
        return c.me
      })
      const { result } = renderHook(() => useSealedConversation(c.sealedKey, [], unlockVault))
      await act(() => result.current.unlock())
      expect(result.current.warning).toMatch(/chat-earlier/)
      render(<SealedConversationBanner state={result.current.state} error={null} onUnlock={vi.fn()}
                                       warning={result.current.warning} onDismissWarning={result.current.dismissWarning} />)
      expect(screen.getByTestId('sealed-self-mismatch').textContent).toMatch(/not yours/)
      act(() => fireEvent.click(screen.getByText('Dismiss')))
      expect(result.current.warning).toBeNull()
      expect(store.get('canopy-vault/v1/self-mismatch')).toBeUndefined()
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
