// @vitest-environment jsdom
//
// Answering an agent's request: paste or load the value, then Seal and store.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const vault = vi.hoisted(() => ({
  getKeyset: vi.fn(),
  getSecretRequest: vi.fn(),
  getZkRecipients: vi.fn(),
}))
vi.mock('@/api/vault', () => vault)
vi.mock('@/vault/operations', () => ({
  endorseReaders: vi.fn(),
  sealAndStore: vi.fn(),
  stepUpAndUnlock: vi.fn(),
  unlockVaultKey: vi.fn(),
}))

const { SecretSealCard } = await import('./SecretSealCard')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const PEM = '-----BEGIN TEST VALUE-----\nMIIE\n-----END TEST VALUE-----'

describe('SecretSealCard', () => {
  it('loads a file into the field, keeping its line breaks, and seals nothing until asked', async () => {
    render(<SecretSealCard slug="bailey" name="BAILEY_GH_APP_KEY" />)
    fireEvent.change(screen.getByTestId('secret-seal-upload'), { target: { files: [new File([`${PEM}\n`], 'app.pem')] } })
    const field = screen.getByLabelText('Value for BAILEY_GH_APP_KEY') as HTMLTextAreaElement
    await waitFor(() => expect(field.value).toBe(PEM))
    expect(field.tagName).toBe('TEXTAREA')
    expect(vault.getKeyset).not.toHaveBeenCalled()
  })

  it('says plainly when the passkey check did not finish', async () => {
    const refused = Object.assign(new Error('The operation either timed out or was not allowed.'), { name: 'NotAllowedError' })
    vault.getKeyset.mockResolvedValue({ exists: false })
    vault.getZkRecipients.mockRejectedValue(refused)
    render(<SecretSealCard slug="bailey" name="BAILEY_GH_APP_ID" />)
    fireEvent.change(screen.getByLabelText('Value for BAILEY_GH_APP_ID'), { target: { value: '12345' } })
    fireEvent.click(screen.getByTestId('secret-seal-submit'))
    expect((await screen.findByTestId('secret-seal-error')).textContent).toMatch(/passkey check did not finish/)
  })
})
