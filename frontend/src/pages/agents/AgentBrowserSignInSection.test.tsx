// @vitest-environment jsdom
//
// Browser sign-in for testing: off by default; an admin switches it on for 1, 7 or 30 days
// (the API asks for the passkey), sees who switched it on and when it ends, and switches it off.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { getBrowserSignIn, switchOnBrowserSignIn, switchOffBrowserSignIn } = vi.hoisted(() => ({
  getBrowserSignIn: vi.fn(),
  switchOnBrowserSignIn: vi.fn(),
  switchOffBrowserSignIn: vi.fn(),
}))
vi.mock('@/api/browserSignIn', () => ({ getBrowserSignIn, switchOnBrowserSignIn, switchOffBrowserSignIn }))

const { AgentBrowserSignInSection } = await import('./AgentBrowserSignInSection')

const OFF = { enabled: false, login_email: 'eva@dimagi-ai.com', max_days: 30, default_days: 7 }
const inDays = (d: number) => new Date(Date.now() + d * 86400_000).toISOString()
const ON = {
  ...OFF,
  enabled: true,
  granted_by_email: 'jj@dimagi.com',
  granted_at: new Date().toISOString(),
  expires_at: inDays(30),
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentBrowserSignInSection', () => {
  it('is off by default', async () => {
    getBrowserSignIn.mockResolvedValue(OFF)
    render(<AgentBrowserSignInSection slug="eva" canEdit />)
    expect((await screen.findByTestId('browser-sign-in-summary')).textContent).toBe('Off')
    expect((screen.getByTestId('browser-sign-in-days') as HTMLSelectElement).value).toBe('7')
    expect(screen.queryByTestId('browser-sign-in-off')).toBeNull()
  })

  it('switches on for the days chosen, then shows who and when it ends', async () => {
    getBrowserSignIn.mockResolvedValue(OFF)
    switchOnBrowserSignIn.mockResolvedValue(ON)
    render(<AgentBrowserSignInSection slug="eva" canEdit />)
    fireEvent.change(await screen.findByTestId('browser-sign-in-days'), { target: { value: '30' } })
    fireEvent.click(screen.getByTestId('browser-sign-in-on'))
    await waitFor(() => expect(switchOnBrowserSignIn).toHaveBeenCalledWith('eva', 30))
    await screen.findByTestId('browser-sign-in-off')
    const summary = screen.getByTestId('browser-sign-in-summary')
    expect(summary.textContent).toContain('switched on by jj@dimagi.com')
    expect(summary.textContent).toMatch(/ends in /)
  })

  it('switches off', async () => {
    getBrowserSignIn.mockResolvedValue(ON)
    switchOffBrowserSignIn.mockResolvedValue(OFF)
    render(<AgentBrowserSignInSection slug="eva" canEdit />)
    fireEvent.click(await screen.findByTestId('browser-sign-in-off'))
    await waitFor(() => expect(switchOffBrowserSignIn).toHaveBeenCalledWith('eva'))
    expect(await screen.findByTestId('browser-sign-in-on')).toBeTruthy()
  })

  it('says so when switching on is refused', async () => {
    getBrowserSignIn.mockResolvedValue(OFF)
    switchOnBrowserSignIn.mockRejectedValue(new Error('Confirm it’s you with your passkey'))
    render(<AgentBrowserSignInSection slug="eva" canEdit />)
    fireEvent.click(await screen.findByTestId('browser-sign-in-on'))
    expect(await screen.findByText(/Confirm it’s you/)).toBeTruthy()
  })

  it('has nothing to switch for an agent with no login of its own', async () => {
    getBrowserSignIn.mockResolvedValue({ ...OFF, login_email: '' })
    render(<AgentBrowserSignInSection slug="eva" canEdit />)
    expect((await screen.findByTestId('browser-sign-in-summary')).textContent).toContain('no canopy login')
    expect(screen.queryByTestId('browser-sign-in-on')).toBeNull()
  })

  it('is for admins only', () => {
    render(<AgentBrowserSignInSection slug="eva" canEdit={false} />)
    expect(screen.queryByTestId('agent-browser-sign-in')).toBeNull()
    expect(getBrowserSignIn).not.toHaveBeenCalled()
  })
})
