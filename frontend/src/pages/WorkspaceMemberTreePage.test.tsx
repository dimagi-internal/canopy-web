// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import type { MemberTreeOut } from '@/api/workspaces'

const getMemberTree = vi.fn<(slug: string) => Promise<MemberTreeOut>>()
const addTreeMember = vi.fn()
const setMemberRole = vi.fn()
const removeMember = vi.fn()

class FakeWorkspaceApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

vi.mock('@/api/workspaces', () => ({
  getMemberTree,
  addTreeMember,
  setMemberRole,
  removeMember,
  WorkspaceApiError: FakeWorkspaceApiError,
}))

vi.mock('@/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ workspaces: [], active: 'org', loading: false, refresh: vi.fn() }),
}))

vi.mock('@/auth/AuthProvider', () => ({
  useAuth: () => ({ status: 'authenticated', user: { email: 'owner@dimagi.com', name: 'O', avatar_url: '' } }),
}))

const { WorkspaceMemberTreePage } = await import('./WorkspaceMemberTreePage')

function tree(): MemberTreeOut {
  return {
    root: 'org',
    workspaces: [
      { slug: 'org', display_name: 'Org', parent: null, depth: 0, my_role: 'owner', can_manage: true },
      { slug: 'connect', display_name: 'Connect', parent: 'org', depth: 1, my_role: 'owner', can_manage: true },
    ],
    people: [
      {
        user_id: 1,
        email: 'owner@dimagi.com',
        cells: {
          org: { role: 'owner', inherited: false, inherited_from: null },
          connect: { role: 'owner', inherited: true, inherited_from: 'org' },
        },
      },
      {
        user_id: 2,
        email: 'wvink@dimagi.com',
        cells: { org: { role: 'editor', inherited: false, inherited_from: null } },
      },
      {
        user_id: 3,
        email: 'sam@dimagi.com',
        cells: {
          org: { role: 'editor', inherited: false, inherited_from: null },
          connect: { role: 'viewer', inherited: false, inherited_from: null },
        },
      },
    ],
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/w/org/settings/member-tree']}>
      <Routes>
        <Route path="/w/:workspace/settings/member-tree" element={<WorkspaceMemberTreePage />} />
        <Route path="/" element={<div>HOME</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('WorkspaceMemberTreePage', () => {
  it('renders a matrix of people by workspace, inherited cells read-only', async () => {
    getMemberTree.mockResolvedValue(tree())
    renderPage()

    expect(await screen.findByText('wvink@dimagi.com')).toBeTruthy()
    expect(screen.getByText('Connect')).toBeTruthy()
    expect(screen.getByText('inherited from Org')).toBeTruthy()
    // The inherited cell is not a control.
    expect(screen.queryByLabelText('owner@dimagi.com in Connect')).toBeNull()
    // The gap is an empty select.
    const gap = screen.getByLabelText('wvink@dimagi.com in Connect') as HTMLSelectElement
    expect(gap.value).toBe('')
  })

  it('picking a role on an empty cell adds them to that workspace', async () => {
    getMemberTree.mockResolvedValue(tree())
    addTreeMember.mockResolvedValue({})
    renderPage()

    fireEvent.change(await screen.findByLabelText('wvink@dimagi.com in Connect'), { target: { value: 'editor' } })

    await waitFor(() => expect(addTreeMember).toHaveBeenCalledWith('connect', 2, 'editor'))
    expect(setMemberRole).not.toHaveBeenCalled()
    await waitFor(() => expect(getMemberTree).toHaveBeenCalledTimes(2))
  })

  it('changing a held role patches it, and a failure shows on the cell', async () => {
    getMemberTree.mockResolvedValue(tree())
    setMemberRole.mockRejectedValue(new Error('cannot demote the last owner'))
    renderPage()

    fireEvent.change(await screen.findByLabelText('sam@dimagi.com in Connect'), {
      target: { value: 'editor' },
    })

    await waitFor(() => expect(setMemberRole).toHaveBeenCalledWith('connect', 3, 'editor'))
    expect((await screen.findByRole('alert')).textContent).toContain('last owner')
  })

  it('only show gaps hides people who are everywhere', async () => {
    getMemberTree.mockResolvedValue(tree())
    renderPage()
    await screen.findByText('wvink@dimagi.com')

    fireEvent.click(screen.getByLabelText(/Only show gaps/))

    expect(screen.getByText('wvink@dimagi.com')).toBeTruthy()
    expect(screen.queryByText('owner@dimagi.com')).toBeNull()
    expect(screen.queryByText('sam@dimagi.com')).toBeNull()
  })
})
