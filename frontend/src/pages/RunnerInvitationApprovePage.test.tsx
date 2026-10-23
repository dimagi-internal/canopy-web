// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import type { RunnerInvitationOut } from '@/api/runnerInvitations'

const getRunnerInvitation = vi.fn<(id: string) => Promise<RunnerInvitationOut>>()
const approveRunnerInvitation = vi.fn<(id: string) => Promise<RunnerInvitationOut>>()

class FakeRunnerInvitationError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

vi.mock('@/api/runnerInvitations', () => ({
  getRunnerInvitation,
  approveRunnerInvitation,
  RunnerInvitationError: FakeRunnerInvitationError,
}))

const { RunnerInvitationApprovePage } = await import('./RunnerInvitationApprovePage')

const ID = '0b4f7f8e-1c2d-4e5f-8a9b-0c1d2e3f4a5b'

function invitation(over: Partial<RunnerInvitationOut> = {}): RunnerInvitationOut {
  return {
    id: ID,
    name: 'cloud-ec2-fork',
    workspace: 'connect',
    role_arn: 'arn:aws:iam::123456789012:role/canopy-cloud-runner-fork-RunnerRole-X',
    status: 'pending',
    approve_url: `https://canopy.dimagi.com/w/connect/runner-invitations/${ID}`,
    created_at: '2026-10-19T10:00:00Z',
    expires_at: '2026-10-19T11:00:00Z',
    created_by_email: 'jj@dimagi.com',
    approved_at: null,
    has_ecdh_key: true,
    runner_id: null,
    directed: false,
    units: [],
    ...over,
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={[`/w/connect/runner-invitations/${ID}`]}>
      <Routes>
        <Route path="/w/:workspace/runner-invitations/:invitationId" element={<RunnerInvitationApprovePage />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerInvitationApprovePage', () => {
  it('shows what is being approved: name, workspace, role, who asked and when it expires', async () => {
    getRunnerInvitation.mockResolvedValue(invitation())
    renderPage()
    expect(await screen.findByText('cloud-ec2-fork')).toBeTruthy()
    expect(screen.getByText('connect')).toBeTruthy()
    expect(screen.getByText('arn:aws:iam::123456789012:role/canopy-cloud-runner-fork-RunnerRole-X')).toBeTruthy()
    expect(screen.getByText('jj@dimagi.com')).toBeTruthy()
    expect(screen.getByText(new Date('2026-10-19T11:00:00Z').toLocaleString())).toBeTruthy()
    expect(getRunnerInvitation).toHaveBeenCalledWith(ID)
  })

  it('lists every unit one approval pairs', async () => {
    getRunnerInvitation.mockResolvedValue(
      invitation({
        units: [
          { name: 'cloud-ec2-9', workspace: 'connect', kind: 'tree', slug: '', has_ecdh_key: false, runner_id: null },
          { name: 'cloud-ec2-9-marsh', workspace: 'marsh', kind: 'tree', slug: '', has_ecdh_key: false, runner_id: null },
          { name: 'cloud-ec2-9-agent-eva', workspace: 'connect', kind: 'agent', slug: 'eva', has_ecdh_key: false, runner_id: null },
        ],
      }),
    )
    renderPage()
    const rows = await screen.findAllByTestId('invitation-unit')
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('cloud-ec2-9its workspace tree'),
      expect.stringContaining('cloud-ec2-9-marsh'),
      expect.stringContaining('only agent eva'),
    ])
    expect(screen.getByText(/each of these 3 units your runner/)).toBeTruthy()
  })

  it('approve calls the approve endpoint and shows the new state', async () => {
    getRunnerInvitation.mockResolvedValue(invitation())
    approveRunnerInvitation.mockResolvedValue(invitation({ status: 'approved', approved_at: '2026-10-19T10:01:00Z' }))
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(approveRunnerInvitation).toHaveBeenCalledWith(ID))
    expect((await screen.findByTestId('invitation-status')).textContent).toContain('approved')
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  })

  it('after approving it says so, re-reads until the box pairs, then links to the runner', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      getRunnerInvitation.mockResolvedValueOnce(invitation())
      approveRunnerInvitation.mockResolvedValue(invitation({ status: 'approved' }))
      getRunnerInvitation.mockResolvedValue(
        invitation({
          status: 'consumed',
          units: [{ name: 'cloud-ec2-fork', workspace: 'connect', kind: 'tree', slug: '', has_ecdh_key: false, runner_id: 'r-9' }],
        }),
      )
      renderPage()
      fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
      expect((await screen.findByRole('status')).textContent).toContain('Waiting for the box to pair')
      await vi.advanceTimersByTimeAsync(5000)
      await waitFor(() =>
        expect(screen.getByRole('status').textContent).toContain('Paired: cloud-ec2-fork is your runner now.'),
      )
      expect(screen.getByRole('link', { name: 'Open its page' }).getAttribute('href')).toBe(
        '/w/connect/runners/r-9',
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('an expired invitation says so and offers no button', async () => {
    getRunnerInvitation.mockResolvedValue(invitation({ status: 'expired' }))
    renderPage()
    expect((await screen.findByTestId('invitation-status')).textContent).toContain('expired')
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  })

  it("someone else's invitation reads as not found", async () => {
    getRunnerInvitation.mockRejectedValue(new FakeRunnerInvitationError(404, 'invitation not found'))
    renderPage()
    expect(await screen.findByText(/No such invitation/)).toBeTruthy()
  })

  it('a refused approve shows why', async () => {
    getRunnerInvitation.mockResolvedValue(invitation())
    approveRunnerInvitation.mockRejectedValue(new Error('this invitation has expired; run up.sh again for a new one'))
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    expect(await screen.findByText(/has expired/)).toBeTruthy()
  })
})

describe('RunnerInvitationApprovePage — a test box', () => {
  it('says the box pairs directed only', async () => {
    getRunnerInvitation.mockResolvedValue(invitation({ directed: true }))
    renderPage()
    expect((await screen.findByTestId('invitation-directed')).textContent).toContain('directed only')
  })
})
