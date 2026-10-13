// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as agentsApi from '@/api/agents'
import * as mail from '@/api/mail'
import * as workspacesApi from '@/api/workspaces'

const domain = {
  id: 1, name: 'marshellis.com', status: 'pending', receiving: false, mx_provider: 'Zoho',
  suspended_reason: '', daily_send_cap: 200, can_manage: true,
  records: [
    { purpose: 'spf', type: 'TXT', name: 'marshellis.com',
      value: 'v=spf1 include:zohomail.com include:amazonses.com -all', priority: null, ok: false, found: [] },
    { purpose: 'mx', type: 'MX', name: 'marshellis.com',
      value: 'inbound-smtp.us-east-1.amazonaws.com', priority: 10, ok: false, found: ['10 mx.zoho.com.'] },
  ],
  routes: [{ id: 9, local_part: '*', address: '*@marshellis.com', kind: 'forward', agent_slug: '',
             forward_to_user_id: 1, forward_to_email: 'jjackson@dimagi.com', enabled: true }],
}

vi.mock('@/api/mail', async (orig) => ({
  ...(await orig<typeof import('@/api/mail')>()),
  listMailDomains: vi.fn(async () => ({ enabled: true, can_manage: true, items: [domain] })),
  saveMailRoute: vi.fn(async () => domain.routes[0]),
  deleteMailRoute: vi.fn(async () => undefined),
  removeMailDomain: vi.fn(async () => undefined),
}))
vi.mock('@/api/agents', () => ({ listAgents: vi.fn(async () => ({ items: [{ slug: 'hal', name: 'Hal', workspace: 'marshellis' }] })) }))
vi.mock('@/api/workspaces', async (orig) => ({
  ...(await orig<object>()),
  listMembers: vi.fn(async () => [{ user_id: 1, email: 'jjackson@dimagi.com', role: 'owner' }]),
}))

const { default: WorkspaceEmailPage } = await import('./WorkspaceEmailPage')

afterEach(() => { cleanup(); vi.clearAllMocks() })

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/w/marshellis/settings/email']}>
      <Routes><Route path="/w/:workspace/settings/email" element={<WorkspaceEmailPage />} /></Routes>
    </MemoryRouter>,
  )
}

describe('WorkspaceEmailPage', () => {
  it('warns before an MX switch takes mail from the current provider', async () => {
    renderPage()
    expect(await screen.findByText(/Zoho currently receives mail for this domain/)).toBeTruthy()
  })
  it('shows the merged SPF value, never a second SPF record', async () => {
    renderPage()
    expect(await screen.findByText('v=spf1 include:zohomail.com include:amazonses.com -all')).toBeTruthy()
  })
  it('lists the catch-all forward to a member', async () => {
    renderPage()
    expect(await screen.findByText('*@marshellis.com')).toBeTruthy()
    expect(screen.getByText('jjackson@dimagi.com')).toBeTruthy()
  })
  it('warns when several SPF records exist', async () => {
    const spf = domain.records[0]
    vi.mocked(mail.listMailDomains).mockResolvedValueOnce({
      enabled: true, can_manage: true,
      items: [{ ...domain, records: [{ ...spf, found: ['v=spf1 include:a -all', 'v=spf1 include:b -all'] }] }],
    })
    renderPage()
    expect(await screen.findByText(/Only one SPF record may exist/)).toBeTruthy()
  })
  it('forwards only to a chosen member, never a typed email', async () => {
    renderPage()
    await screen.findByText('*@marshellis.com')
    fireEvent.change(screen.getByLabelText('Local part'), { target: { value: 'jj' } })
    fireEvent.change(screen.getByLabelText('Route kind'), { target: { value: 'forward' } })
    expect(screen.queryByPlaceholderText(/@/)).toBeNull()
    fireEvent.change(screen.getByLabelText('Member'), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save address' }))
    await vi.waitFor(() => expect(mail.saveMailRoute).toHaveBeenCalledWith('marshellis', 1, {
      local_part: 'jj', kind: 'forward', agent_slug: '', forward_to_user_id: 1, enabled: true }))
  })
  it('confirms before removing an address, saying threads are kept', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    renderPage()
    await screen.findByText('*@marshellis.com')
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(confirm).toHaveBeenLastCalledWith(
      'Remove *@marshellis.com? It stops receiving mail. Existing threads are kept.')
    expect(mail.deleteMailRoute).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    await vi.waitFor(() => expect(mail.deleteMailRoute).toHaveBeenCalledWith('marshellis', 1, 9))
    confirm.mockRestore()
  })
  it('confirms before removing a domain, saying threads are kept', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false)
    renderPage()
    await screen.findByText('*@marshellis.com')
    fireEvent.click(screen.getByRole('button', { name: 'Remove domain' }))
    expect(confirm).toHaveBeenLastCalledWith(
      'Remove marshellis.com? Its addresses stop receiving mail. Existing threads are kept.')
    expect(mail.removeMailDomain).not.toHaveBeenCalled()
    confirm.mockRestore()
  })
  it('hides everything but a note when email is off', async () => {
    vi.mocked(mail.listMailDomains).mockResolvedValueOnce({ enabled: false, can_manage: true, items: [] })
    renderPage()
    expect(await screen.findByText(/Email domains are off on this deployment/)).toBeTruthy()
  })
  it('shows the Add domain form only when the list says the caller may manage', async () => {
    vi.mocked(mail.listMailDomains).mockResolvedValueOnce({ enabled: true, can_manage: false, items: [] })
    const first = renderPage()
    await screen.findByText(/Run your own domain/)
    expect(screen.queryByRole('button', { name: 'Add domain' })).toBeNull()
    first.unmount()
    vi.mocked(mail.listMailDomains).mockResolvedValueOnce({ enabled: true, can_manage: true, items: [] })
    renderPage()
    expect(await screen.findByRole('button', { name: 'Add domain' })).toBeTruthy()
  })
  it('editing a disabled address keeps it disabled', async () => {
    vi.mocked(mail.listMailDomains).mockResolvedValueOnce({
      enabled: true, can_manage: true, items: [{ ...domain, routes: [{ ...domain.routes[0], enabled: false }] }],
    })
    renderPage()
    await screen.findByText('*@marshellis.com')
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save address' }))
    await vi.waitFor(() => expect(mail.saveMailRoute).toHaveBeenCalledWith('marshellis', 1, {
      local_part: '*', kind: 'forward', agent_slug: '', forward_to_user_id: 1, enabled: false }))
  })
  it('an admin toggles an address on and off through the same save', async () => {
    renderPage()
    const box = await screen.findByLabelText('Enabled: *@marshellis.com')
    expect((box as HTMLInputElement).checked).toBe(true)
    fireEvent.click(box)
    await vi.waitFor(() => expect(mail.saveMailRoute).toHaveBeenCalledWith('marshellis', 1, {
      local_part: '*', kind: 'forward', agent_slug: '', forward_to_user_id: 1, enabled: false }))
  })
  it('a non-admin sees Enabled as text, with no toggle', async () => {
    vi.mocked(mail.listMailDomains).mockResolvedValueOnce({
      enabled: true, can_manage: false, items: [{ ...domain, can_manage: false }],
    })
    renderPage()
    await screen.findByText('*@marshellis.com')
    expect(screen.queryByLabelText('Enabled: *@marshellis.com')).toBeNull()
    expect(screen.getByText('Yes')).toBeTruthy()
  })
  it('says so when agents or members fail to load', async () => {
    vi.mocked(agentsApi.listAgents).mockRejectedValueOnce(new Error('boom'))
    vi.mocked(workspacesApi.listMembers).mockRejectedValueOnce(new Error('nope'))
    renderPage()
    const err = await screen.findByTestId('mail-lookup-error')
    await vi.waitFor(() => expect(err.textContent).toContain('Could not load members: nope'))
    expect(err.textContent).toContain('Could not load agents: boom')
  })
  it('a Copy button says Copied once the value is on the clipboard', async () => {
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    renderPage()
    const btn = await screen.findByRole('button', { name: 'Copy spf value' })
    expect(btn.textContent).toBe('Copy')
    fireEvent.click(btn)
    expect(writeText).toHaveBeenCalledWith('v=spf1 include:zohomail.com include:amazonses.com -all')
    await vi.waitFor(() => expect(btn.textContent).toBe('Copied'))
  })
  it('the form has its own Enabled control, so a disabled edit is visible and reversible', async () => {
    vi.mocked(mail.listMailDomains).mockResolvedValueOnce({
      enabled: true, can_manage: true, items: [{ ...domain, routes: [{ ...domain.routes[0], enabled: false }] }],
    })
    renderPage()
    await screen.findByText('*@marshellis.com')
    const control = screen.getByLabelText('Enabled') as HTMLInputElement
    expect(control.checked).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(control.checked).toBe(false)
    fireEvent.click(control)
    fireEvent.change(screen.getByLabelText('Local part'), { target: { value: 'new' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save address' }))
    await vi.waitFor(() => expect(mail.saveMailRoute).toHaveBeenCalledWith('marshellis', 1, {
      local_part: 'new', kind: 'forward', agent_slug: '', forward_to_user_id: 1, enabled: true }))
  })
})
