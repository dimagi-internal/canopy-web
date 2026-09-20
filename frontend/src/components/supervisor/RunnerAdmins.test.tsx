// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RunnerAdmins } from './RunnerAdmins'

// The grant existed with no page, so adding an administrator meant a curl with
// the pairer's token. What this panel has to get right is the two tiers: an
// administrator may READ the list, only the pairer may change it — and a refused
// read must not render as "nobody administers this box".

const list = vi.fn()
const grant = vi.fn()
const revoke = vi.fn()

vi.mock('@/api/harness', () => ({
  listRunnerAdmins: (...a: unknown[]) => list(...a),
  grantRunnerAdmin: (...a: unknown[]) => grant(...a),
  revokeRunnerAdmin: (...a: unknown[]) => revoke(...a),
}))

const ROW = { user_id: 4, email: 'smazumdar@dimagi.com', granted_by_email: 'jjackson@dimagi.com', created_at: '' }

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerAdmins', () => {
  it('lets the pairer grant, and shows who holds a grant', async () => {
    list.mockResolvedValue([ROW])
    grant.mockResolvedValue(ROW)
    render(<RunnerAdmins runnerId="r1" canManage pairedByEmail="jjackson@dimagi.com" />)
    expect(await screen.findByText('smazumdar@dimagi.com')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Grant administration to'),
                     { target: { value: 'stewari@dimagi.com' } })
    fireEvent.click(screen.getByTestId('runner-admins-grant'))
    await waitFor(() => expect(grant).toHaveBeenCalledWith('r1', 'stewari@dimagi.com'))
    // Re-read rather than trusting the local list: the server decides.
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2))
  })

  it('shows an administrator the list without any way to change it', async () => {
    list.mockResolvedValue([ROW])
    render(<RunnerAdmins runnerId="r1" canManage={false} />)
    expect(await screen.findByText('smazumdar@dimagi.com')).toBeTruthy()
    expect(screen.queryByTestId('runner-admins-grant')).toBeNull()
    expect(screen.queryByLabelText(`Revoke ${ROW.email}`)).toBeNull()
  })

  it('says a refused read is refused, not empty', async () => {
    list.mockRejectedValue(new Error('404'))
    render(<RunnerAdmins runnerId="r1" canManage={false} />)
    expect(await screen.findByTestId('runner-admins-refused')).toBeTruthy()
    expect(screen.queryByTestId('runner-admins-empty')).toBeNull()
  })

  it('keeps the server’s reason when a grant is refused', async () => {
    list.mockResolvedValue([])
    grant.mockRejectedValue(new Error('aking@dimagi.com is not a member of the workspace this runner belongs to'))
    render(<RunnerAdmins runnerId="r1" canManage />)
    await screen.findByTestId('runner-admins-empty')
    fireEvent.change(screen.getByLabelText('Grant administration to'),
                     { target: { value: 'aking@dimagi.com' } })
    fireEvent.click(screen.getByTestId('runner-admins-grant'))
    expect((await screen.findByTestId('runner-admins-error')).textContent).toContain('not a member of the workspace')
  })
})
