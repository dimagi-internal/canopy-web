// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RunnerAdmins } from './RunnerAdmins'

// The grant existed with no page, so adding an administrator meant a curl with
// the owner's token. What this panel has to get right is the two tiers: an
// administrator may READ the list, only the owner may change it — and a refused
// read must not render as "nobody administers this box".

const list = vi.fn()
const grant = vi.fn()
const revoke = vi.fn()

const members = vi.fn()
vi.mock('@/api/workspaces', () => ({
  listMembers: (...a: unknown[]) => members(...a),
}))

const MEMBERS = [
  { user_id: 1, email: 'jjackson@dimagi.com', role: 'owner', joined_at: '' },
  { user_id: 4, email: 'smazumdar@dimagi.com', role: 'editor', joined_at: '' },
  { user_id: 5, email: 'stewari@dimagi.com', role: 'editor', joined_at: '' },
  { user_id: 6, email: 'aking@dimagi.com', role: 'viewer', joined_at: '' },
]

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
  it('lets the owner grant, and shows who holds a grant', async () => {
    list.mockResolvedValue([ROW])
    grant.mockResolvedValue(ROW)
    members.mockResolvedValue(MEMBERS)
    render(<RunnerAdmins runnerId="r1" workspace="dimagi" canManage ownerEmail="jjackson@dimagi.com" />)
    expect(await screen.findByText('smazumdar@dimagi.com')).toBeTruthy()
    expect(members).toHaveBeenCalledWith('dimagi')

    fireEvent.change(await screen.findByLabelText('Person to make an administrator'),
                     { target: { value: 'stewari@dimagi.com' } })
    fireEvent.click(screen.getByTestId('runner-admins-grant'))
    await waitFor(() => expect(grant).toHaveBeenCalledWith('r1', 'stewari@dimagi.com'))
    // Re-read rather than trusting the local list: the server decides.
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2))
  })

  it('shows the owner as a fixed role row and lets the owner remove an admin', async () => {
    list.mockResolvedValue([ROW])
    revoke.mockResolvedValue(undefined)
    render(<RunnerAdmins runnerId="r1" canManage ownerEmail="jjackson@dimagi.com" />)
    const owner = await screen.findByTestId('runner-admin-owner')
    expect(owner.textContent).toContain('Owner')
    expect(owner.textContent).toContain('owns this runner')
    expect(screen.getByTestId('runner-admin-4').textContent).toContain('Admin')
    expect(screen.getByTestId('runner-admin-4').textContent).toContain('granted by jjackson@dimagi.com')
    // a single grantable role reads as text, never a one-option dropdown
    expect(screen.getByTestId('runner-admin-4').querySelector('select')).toBeNull()

    fireEvent.click(screen.getByLabelText(`Remove ${ROW.email}`))
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('r1', 4))
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2))
  })

  it('shows an administrator the list without any way to change it', async () => {
    list.mockResolvedValue([ROW])
    render(<RunnerAdmins runnerId="r1" canManage={false} />)
    expect(await screen.findByText('smazumdar@dimagi.com')).toBeTruthy()
    expect(screen.queryByTestId('runner-admins-grant')).toBeNull()
    expect(screen.queryByLabelText(`Remove ${ROW.email}`)).toBeNull()
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
    members.mockResolvedValue(MEMBERS)
    render(<RunnerAdmins runnerId="r1" workspace="dimagi" canManage />)
    await screen.findByTestId('runner-admins-empty')
    await screen.findByRole('option', { name: 'aking@dimagi.com' })
    fireEvent.change(screen.getByLabelText('Person to make an administrator'),
                     { target: { value: 'aking@dimagi.com' } })
    fireEvent.click(screen.getByTestId('runner-admins-grant'))
    expect((await screen.findByTestId('runner-admins-error')).textContent).toContain('not a member of the workspace')
  })

  it('offers only people in the workspace who are not already on the list', async () => {
    list.mockResolvedValue([ROW])
    members.mockResolvedValue(MEMBERS)
    render(<RunnerAdmins runnerId="r1" workspace="dimagi" canManage ownerEmail="jjackson@dimagi.com" />)
    await screen.findByRole('option', { name: 'stewari@dimagi.com' })
    const offered = screen.getAllByRole('option').map((o) => o.textContent).slice(1)
    expect(offered).toEqual(['aking@dimagi.com', 'stewari@dimagi.com'])
    // Nothing to send until someone is picked.
    expect((screen.getByTestId('runner-admins-grant') as HTMLButtonElement).disabled).toBe(true)
  })
})
