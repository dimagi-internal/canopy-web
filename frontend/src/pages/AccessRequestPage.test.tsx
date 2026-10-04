// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import type { AccessRequestOut } from '@/api/workspaces'

const getAccessRequest = vi.fn<(slug: string, id: number) => Promise<AccessRequestOut>>()
const approveAccessRequest = vi.fn<(slug: string, id: number, role: string) => Promise<AccessRequestOut>>()
const denyAccessRequest = vi.fn<(slug: string, id: number, reason: string) => Promise<AccessRequestOut>>()
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
  getAccessRequest,
  approveAccessRequest,
  denyAccessRequest,
  setMemberRole,
  removeMember,
  WorkspaceApiError: FakeWorkspaceApiError,
}))

let mockRole = 'owner'
vi.mock('@/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    workspaces: [{ slug: 'acme', display_name: 'Acme', role: mockRole }],
    active: 'acme',
    loading: false,
    refresh: vi.fn(),
  }),
}))

const { AccessRequestPage } = await import('./AccessRequestPage')

function request(over: Partial<AccessRequestOut> = {}): AccessRequestOut {
  return {
    id: 7,
    workspace: 'acme',
    workspace_display_name: 'Acme',
    user_id: 42,
    email: 'ada@dimagi.com',
    name: 'Ada Lovelace',
    note: 'I run the demos',
    status: 'pending',
    role: '',
    auto: false,
    decided_by_email: null,
    decided_at: null,
    decision_reason: '',
    created_at: '2026-10-04T10:00:00Z',
    current_role: null,
    notify_result: { emailed: ['owner@dimagi.com'], failed: [], recipients: 1 },
    ...over,
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/w/acme/settings/access-requests/7']}>
      <Routes>
        <Route path="/w/:workspace/settings/access-requests/:requestId" element={<AccessRequestPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  mockRole = 'owner'
})

describe('AccessRequestPage', () => {
  it('shows who asked, their domain and note', async () => {
    getAccessRequest.mockResolvedValue(request())
    renderPage()
    expect(await screen.findByText(/Ada Lovelace \(ada@dimagi.com\) requested an invitation/)).toBeTruthy()
    expect(screen.getByText('dimagi.com')).toBeTruthy()
    expect(screen.getByText('I run the demos')).toBeTruthy()
  })

  it.each(['viewer', 'editor', 'admin'] as const)('an owner approves as %s', async (role) => {
    getAccessRequest.mockResolvedValue(request())
    approveAccessRequest.mockResolvedValue(
      request({ status: 'approved', role, decided_by_email: 'owner@dimagi.com', current_role: role }),
    )
    renderPage()
    const picker = (await screen.findByLabelText(/let them in as/i)) as HTMLSelectElement
    expect(picker.value).toBe('viewer') // default
    fireEvent.change(picker, { target: { value: role } })
    fireEvent.click(screen.getByRole('button', { name: /^approve$/i }))
    await waitFor(() => expect(approveAccessRequest).toHaveBeenCalledWith('acme', 7, role))
    expect(await screen.findByText(new RegExp(`Approved as ${role} by owner@dimagi.com`))).toBeTruthy()
    expect(screen.getByText(/has been emailed/)).toBeTruthy()
    expect(screen.getAllByText(/All access requests/).length).toBeGreaterThan(0)
  })

  it('an admin can only pick roles below their own', async () => {
    mockRole = 'admin'
    getAccessRequest.mockResolvedValue(request())
    renderPage()
    const picker = (await screen.findByLabelText(/let them in as/i)) as HTMLSelectElement
    const options = Array.from(picker.options).map((o) => o.value)
    expect(options).toEqual(['viewer', 'editor'])
  })

  it('never offers owner as an approval role', async () => {
    getAccessRequest.mockResolvedValue(request())
    renderPage()
    const picker = (await screen.findByLabelText(/let them in as/i)) as HTMLSelectElement
    expect(Array.from(picker.options).map((o) => o.value)).toEqual(['viewer', 'editor', 'admin'])
  })

  it('deny sends the optional reason', async () => {
    getAccessRequest.mockResolvedValue(request())
    denyAccessRequest.mockResolvedValue(
      request({ status: 'denied', decided_by_email: 'owner@dimagi.com', decision_reason: 'ask Jane' }),
    )
    renderPage()
    fireEvent.change(await screen.findByLabelText(/reason/i), { target: { value: 'ask Jane' } })
    fireEvent.click(screen.getByRole('button', { name: /^deny$/i }))
    await waitFor(() => expect(denyAccessRequest).toHaveBeenCalledWith('acme', 7, 'ask Jane'))
    expect(await screen.findByText(/Denied by owner@dimagi.com/)).toBeTruthy()
    expect(screen.getByText(/ask Jane/)).toBeTruthy()
  })

  it('a decided request is read-only', async () => {
    getAccessRequest.mockResolvedValue(
      request({ status: 'denied', decided_by_email: 'admin@dimagi.com', decided_at: '2026-10-04T11:00:00Z' }),
    )
    renderPage()
    expect(await screen.findByText(/Denied by admin@dimagi.com/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^approve$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^deny$/i })).toBeNull()
    expect(screen.queryByLabelText(/let them in as/i)).toBeNull()
  })

  it('an auto-approved request offers changing the role or removing them', async () => {
    getAccessRequest.mockResolvedValue(
      request({ status: 'approved', role: 'editor', auto: true, current_role: 'editor' }),
    )
    setMemberRole.mockResolvedValue({ user_id: 42, email: 'ada@dimagi.com', role: 'viewer', joined_at: '' })
    renderPage()
    expect(await screen.findByText(/Approved as editor automatically/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText(/^role$/i), { target: { value: 'viewer' } })
    await waitFor(() => expect(setMemberRole).toHaveBeenCalledWith('acme', 42, 'viewer'))
    expect(await screen.findByText(/is now viewer/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /remove from workspace/i })).toBeTruthy()
  })

  it('an editor is told only admins decide', async () => {
    mockRole = 'editor'
    renderPage()
    expect(await screen.findByText(/Only this workspace's admins and owners decide/)).toBeTruthy()
    expect(getAccessRequest).not.toHaveBeenCalled()
  })
})
