// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const listMySignInRequests = vi.fn()
vi.mock('@/api/harness', async (orig) => ({
  ...(await orig<typeof import('@/api/harness')>()),
  listMySignInRequests: (...a: unknown[]) => listMySignInRequests(...a),
}))

const { SignInRequests } = await import('./SignInRequests')

afterEach(() => {
  cleanup()
  listMySignInRequests.mockReset()
})

const URL_ = 'https://commcare-connect.awsapps.com/start/#/device?user_code=ABCD-EFGH'

function req(over: object = {}) {
  return {
    id: 'r1',
    runner_id: 'run1',
    runner_name: 'haldimagi-mbp-cdp',
    provider: 'aws',
    label: 'labs',
    requested_by: 'Ada',
    reason: 'move ace-web to its own Redis DB',
    url: URL_,
    sent: 1,
    created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 9 * 60_000).toISOString(),
    ...over,
  }
}

describe('SignInRequests', () => {
  it('shows who is asking, where, why, and the approval link', async () => {
    listMySignInRequests.mockResolvedValue([req()])
    render(<SignInRequests />)
    expect(await screen.findByText('Ada needs AWS sign-in (labs)')).toBeTruthy()
    expect(screen.getByText('On haldimagi-mbp-cdp: move ace-web to its own Redis DB')).toBeTruthy()
    const link = screen.getByTestId('sign-in-approve') as HTMLAnchorElement
    expect(link.href).toBe(URL_)
    expect(link.target).toBe('_blank')
    expect(screen.getByText('Expires in about 9 min')).toBeTruthy()
  })

  it('renders nothing when no box is waiting', async () => {
    listMySignInRequests.mockResolvedValue([])
    const { container } = render(<SignInRequests />)
    await vi.waitFor(() => expect(listMySignInRequests).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
  })

  it('does not offer a link whose code has already expired', async () => {
    listMySignInRequests.mockResolvedValue([req({ expires_at: new Date(Date.now() - 1000).toISOString() })])
    const { container } = render(<SignInRequests />)
    await vi.waitFor(() => expect(listMySignInRequests).toHaveBeenCalled())
    expect(container.querySelector('[data-testid="sign-in-request"]')).toBeNull()
  })
})
