// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const listProjectMembers = vi.fn()
const addProjectMember = vi.fn()
const removeProjectMember = vi.fn()
vi.mock('@/api/work', async (orig) => ({
  ...(await orig<typeof import('@/api/work')>()),
  listProjectMembers: (...a: unknown[]) => listProjectMembers(...a),
  addProjectMember: (...a: unknown[]) => addProjectMember(...a),
  removeProjectMember: (...a: unknown[]) => removeProjectMember(...a),
}))

const { ProjectMembers } = await import('./ProjectMembers')

afterEach(() => {
  cleanup()
  for (const m of [listProjectMembers, addProjectMember, removeProjectMember]) m.mockReset()
})

const lead = { user_id: 1, email: 'lead@dimagi.com', role: 'owner', sources: ['lead'] }
const ann = { user_id: 2, email: 'ann@dimagi.com', role: 'member', sources: ['direct'] }
const show = () =>
  render(<ProjectMembers detail={{ vaults: [{}] } as never} workspace="dimagi" handle="podcast" onChanged={() => {}} />)

describe('ProjectMembers', () => {
  it('shows a reader who is on it, with no way to change the list', async () => {
    listProjectMembers.mockResolvedValue({ members: [lead, ann], may_manage: false, box_owners_missing: [] })
    show()
    const list = await screen.findByTestId('project-members')
    expect(list.textContent).toContain('lead@dimagi.com')
    expect(list.textContent).toContain('Lead')
    expect(screen.queryByTestId('add-member')).toBeNull()
    expect(screen.queryByText('Remove')).toBeNull()
  })

  it('lets a manager remove someone added by name, but not the lead', async () => {
    listProjectMembers.mockResolvedValue({ members: [lead, ann], may_manage: true, box_owners_missing: [] })
    removeProjectMember.mockResolvedValue({ members: [lead], may_manage: true, box_owners_missing: [] })
    show()
    const [remove, ...others] = await screen.findAllByText('Remove')
    expect(others).toEqual([])
    fireEvent.click(remove)
    await waitFor(() => expect(removeProjectMember).toHaveBeenCalledWith('dimagi', 'podcast', 2))
    await waitFor(() => expect(screen.queryByText('ann@dimagi.com')).toBeNull())
  })

  it('adds someone by address with the role chosen', async () => {
    listProjectMembers.mockResolvedValue({ members: [lead], may_manage: true, box_owners_missing: [] })
    addProjectMember.mockResolvedValue({ members: [lead, ann], may_manage: true, box_owners_missing: [] })
    show()
    fireEvent.change(await screen.findByLabelText('Email'), { target: { value: 'ann@dimagi.com' } })
    fireEvent.change(screen.getByLabelText('Role'), { target: { value: 'admin' } })
    fireEvent.submit(screen.getByTestId('add-member'))
    await waitFor(() => expect(addProjectMember).toHaveBeenCalledWith('dimagi', 'podcast', 'ann@dimagi.com', 'admin'))
  })

  it('names whose machines cannot read its vaults, and adds them in one click', async () => {
    listProjectMembers.mockResolvedValue({
      members: [lead],
      may_manage: true,
      box_owners_missing: [{ email: 'amie@dimagi.com', agents: ['muse'] }],
    })
    addProjectMember.mockResolvedValue({ members: [lead], may_manage: true, box_owners_missing: [] })
    show()
    const box = await screen.findByTestId('box-owners-missing')
    expect(box.textContent).toContain('amie@dimagi.com')
    expect(box.textContent).toContain('muse')
    fireEvent.click(box.querySelector('button') as HTMLButtonElement)
    await waitFor(() => expect(addProjectMember).toHaveBeenCalledWith('dimagi', 'podcast', 'amie@dimagi.com', 'member'))
  })
})
