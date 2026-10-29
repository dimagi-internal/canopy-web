// @vitest-environment jsdom
// Step-up goes straight to the browser's passkey sheet; the "Confirm it's you"
// dialog is only the fallback for a browser that refuses WebAuthn without a
// fresh click (canopy-web#1538).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

const passkeys = vi.hoisted(() => ({ performStepUp: vi.fn(), registerThisDevice: vi.fn() }))
vi.mock('@/api/passkeys', () => passkeys)

const { StepUpDialog } = await import('./StepUpDialog')
const { requestStepUp } = await import('./stepUp')
const { setSessionPasskeys } = await import('./passkeyHere')

function notAllowed(): Error {
  const e = new Error('not allowed')
  e.name = 'NotAllowedError'
  return e
}

beforeEach(() => {
  vi.stubGlobal('PublicKeyCredential', function PublicKeyCredential() {})
  Object.defineProperty(navigator, 'credentials', { value: {}, configurable: true })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('StepUpDialog', () => {
  it('in the Runner app, opens this page in the browser instead of asking for a passkey', async () => {
    setSessionPasskeys(false)
    const open = vi.fn()
    vi.stubGlobal('open', open)
    try {
      render(<StepUpDialog />)
      const asked = requestStepUp({ hasPasskey: true })
      fireEvent.click(await screen.findByTestId('step-up-in-browser'))
      expect(open).toHaveBeenCalledWith(window.location.href, '_blank', 'noopener')
      expect(passkeys.performStepUp).not.toHaveBeenCalled()
      expect(screen.getByTestId('step-up-handed-off')).toBeTruthy()
      fireEvent.click(screen.getByTestId('step-up-cancel'))
      await expect(asked).rejects.toThrow('cancelled')
    } finally {
      setSessionPasskeys(true)
    }
  })

  it('hands a browser_session_required refusal to the browser too', async () => {
    render(<StepUpDialog />)
    const asked = requestStepUp({ hasPasskey: true, inBrowser: true })
    expect(await screen.findByTestId('step-up-in-browser')).toBeTruthy()
    expect(passkeys.performStepUp).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('step-up-cancel'))
    await expect(asked).rejects.toThrow('cancelled')
  })

  it('asks for the passkey directly, with no dialog', async () => {
    passkeys.performStepUp.mockResolvedValue({})
    render(<StepUpDialog />)
    await requestStepUp({ hasPasskey: true })
    expect(passkeys.performStepUp).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('step-up-dialog')).toBeNull()
  })

  it('falls back to the dialog when the browser refuses without a click', async () => {
    passkeys.performStepUp.mockRejectedValueOnce(notAllowed())
    render(<StepUpDialog />)
    const asked = requestStepUp({ hasPasskey: true })
    expect(await screen.findByTestId('step-up-dialog')).toBeTruthy()
    fireEvent.click(screen.getByText('Cancel'))
    await expect(asked).rejects.toThrow('cancelled')
  })

  it('treats a slow dismissal as a cancel, without the dialog', async () => {
    const now = vi.spyOn(Date, 'now')
    now.mockReturnValueOnce(0).mockReturnValueOnce(10_000)
    passkeys.performStepUp.mockRejectedValueOnce(notAllowed())
    render(<StepUpDialog />)
    await expect(requestStepUp({ hasPasskey: true })).rejects.toThrow('cancelled')
    expect(screen.queryByTestId('step-up-dialog')).toBeNull()
  })

  it('still offers to register a passkey through the dialog', async () => {
    render(<StepUpDialog />)
    const asked = requestStepUp({ hasPasskey: false })
    expect(await screen.findByText('Set up a passkey for this device')).toBeTruthy()
    expect(passkeys.performStepUp).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Cancel'))
    await expect(asked).rejects.toThrow('cancelled')
  })
})
