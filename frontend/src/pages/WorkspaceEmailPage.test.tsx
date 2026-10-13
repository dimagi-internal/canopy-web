// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as mail from '@/api/mail'

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
  listMailDomains: vi.fn(async () => ({ enabled: true, items: [domain] })),
  saveMailRoute: vi.fn(async () => domain.routes[0]),
}))
vi.mock('@/api/agents', () => ({ listAgents: vi.fn(async () => ({ items: [{ slug: 'hal', name: 'Hal', workspace: 'marshellis' }] })) }))
vi.mock('@/api/workspaces', async (orig) => ({
  ...(await orig<object>()),
  listMembers: vi.fn(async () => [{ user_id: 1, email: 'jjackson@dimagi.com', role: 'owner' }]),
}))

const { default: WorkspaceEmailPage } = await import('./WorkspaceEmailPage')

afterEach(() => cleanup())

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
      enabled: true,
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
  it('hides everything but a note when email is off', async () => {
    vi.mocked(mail.listMailDomains).mockResolvedValueOnce({ enabled: false, items: [] })
    renderPage()
    expect(await screen.findByText(/Email domains are off on this deployment/)).toBeTruthy()
  })
})
