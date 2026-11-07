// @vitest-environment jsdom
//
// In the Canopy Runner app no passkey can run, so both ways of handing over a
// secret say so up front and open the page in the browser — instead of taking
// the value and failing on the final click.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

const here = vi.hoisted(() => ({ works: false, opened: [] as string[] }))
vi.mock('@/auth/passkeyHere', () => ({
  passkeysWorkHere: () => here.works,
  openInBrowser: (target: string) => here.opened.push(target),
}))
vi.mock('./passkeyHere', () => ({
  passkeysWorkHere: () => here.works,
  openInBrowser: (target: string) => here.opened.push(target),
}))
vi.mock('@/api/vault', () => ({
  getKeyset: vi.fn(),
  getSecretRequest: vi.fn(() => new Promise(() => {})),
  getZkRecipients: vi.fn(() => new Promise(() => {})),
}))
vi.mock('@/vault/operations', () => ({
  endorseReaders: vi.fn(), sealAndStore: vi.fn(), stepUpAndUnlock: vi.fn(), unlockVaultKey: vi.fn(),
}))
vi.mock('@/api/chat', () => ({
  listSecrets: vi.fn(() => Promise.resolve([])),
  getSecretRecipients: vi.fn(() => new Promise(() => {})),
  shareSecret: vi.fn(),
  deleteSecret: vi.fn(),
}))

const { SecretSealCard } = await import('@/components/vault/SecretSealCard')
const { ShareSecretForm } = await import('@/components/chat/ShareSecret')

afterEach(() => {
  cleanup()
  here.works = false
  here.opened = []
})

describe('a secret where no passkey can run', () => {
  it('the chat Secrets form offers the browser instead of a value field', () => {
    render(<ShareSecretForm sessionId="s1" onDone={() => {}} />)
    expect(screen.queryByTestId('secret-value')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /open in browser/i }))
    expect(here.opened).toEqual([window.location.href])
  })

  it("an agent's request card offers the browser instead of a value field", () => {
    render(<SecretSealCard slug="bailey" name="GH_TOKEN" requestId="r1" />)
    expect(screen.queryByTestId('secret-seal-value')).toBeNull()
    expect(screen.getByTestId('passkey-in-browser')).toBeTruthy()
  })

  it('in a browser, both still take the value', () => {
    here.works = true
    render(<ShareSecretForm sessionId="s1" onDone={() => {}} />)
    expect(screen.getByTestId('secret-value')).toBeTruthy()
    cleanup()
    render(<SecretSealCard slug="bailey" name="GH_TOKEN" />)
    expect(screen.getByTestId('secret-seal-value')).toBeTruthy()
  })
})
