// @vitest-environment jsdom
import { renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getKeyset = vi.fn()
vi.mock('@/api/vault', () => ({ getKeyset: () => getKeyset() }))
const stepUpAndUnlock = vi.fn()
const unlockVaultKey = vi.fn()
vi.mock('./operations', () => ({
  stepUpAndUnlock: (...a: unknown[]) => stepUpAndUnlock(...a),
  unlockVaultKey: (...a: unknown[]) => unlockVaultKey(...a),
}))
vi.mock('./trust', () => ({ rememberSelf: () => Promise.resolve() }))
vi.mock('./UnlockPrompt', () => ({ useUnlockPrompt: () => ({ prompt: vi.fn(), element: null }) }))

import { useVaultUnlock } from './useVaultUnlock'

const ME = { userId: '7', scalar: new Uint8Array(1), publicJwk: {}, privateKey: {} }
const BOTH = { seals: [{ alg: 'prf' }, { alg: '2skd' }] }

beforeEach(() => {
  vi.clearAllMocks()
  getKeyset.mockResolvedValue(BOTH)
  unlockVaultKey.mockResolvedValue(ME)
})

describe('useVaultUnlock', () => {
  it('opens the passphrase dialog after a dismissed passkey when asked by a click', async () => {
    stepUpAndUnlock.mockRejectedValue(new DOMException('no', 'NotAllowedError'))
    const { result } = renderHook(() => useVaultUnlock())
    await result.current.unlock()
    expect(unlockVaultKey).toHaveBeenCalledTimes(1)
  })

  it('passkeyOnly: a dismissed passkey never opens the passphrase dialog', async () => {
    stepUpAndUnlock.mockRejectedValue(new DOMException('no', 'NotAllowedError'))
    const { result } = renderHook(() => useVaultUnlock())
    await expect(result.current.unlock({ passkeyOnly: true })).rejects.toMatchObject({ name: 'NotAllowedError' })
    expect(unlockVaultKey).not.toHaveBeenCalled()
  })

  it('passkeyOnly: a key with no passkey seal waits for a click rather than prompting', async () => {
    getKeyset.mockResolvedValue({ seals: [{ alg: '2skd' }] })
    const { result } = renderHook(() => useVaultUnlock())
    await expect(result.current.unlock({ passkeyOnly: true })).rejects.toMatchObject({ name: 'NotAllowedError' })
    expect(unlockVaultKey).not.toHaveBeenCalled()
  })

  it('passkeyOnly still opens it when the passkey works', async () => {
    stepUpAndUnlock.mockResolvedValue(ME)
    const { result } = renderHook(() => useVaultUnlock())
    expect(await result.current.unlock({ passkeyOnly: true })).toBe(ME)
  })
})
