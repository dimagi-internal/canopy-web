// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const { fetchMailThreads, fetchMailThread } = vi.hoisted(() => ({
  fetchMailThreads: vi.fn(),
  fetchMailThread: vi.fn(),
}))
vi.mock('@/api/mailThreads', () => ({ fetchMailThreads, fetchMailThread }))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({ agent: { slug: 'echo', name: 'Echo', workspace: 'connect' } }),
}))

const { AgentMailSection } = await import('./AgentMailSection')

afterEach(() => { cleanup(); vi.clearAllMocks() })

const thread = {
  thread_id: 't1', address: 'echo@mail.example.org', agent: 'echo', subject: 'Grant question',
  message_count: 2, unread: true, archived: false, last_message_at: '2026-10-11T10:00:00Z',
}
const detail = (html = '') => ({
  ...thread,
  messages: [
    { id: 1, direction: 'in', message_id: 'a', from_addr: 'amy@x.org', from_name: 'Amy', to: ['echo@mail.example.org'],
      cc: [], subject: 'Grant question', text: 'First hello', html, delivery_status: '',
      created_at: '2026-10-11T09:00:00Z',
      attachments: [{ filename: 'budget.pdf', content_type: 'application/pdf', size: 10, url: 'https://s3/budget?sig=1' }] },
    { id: 2, direction: 'out', message_id: 'b', from_addr: 'echo@mail.example.org', from_name: 'Echo', to: ['amy@x.org'],
      cc: [], subject: 'Re: Grant question', text: 'Second reply', html: '', delivery_status: 'delivered',
      created_at: '2026-10-11T10:00:00Z', attachments: [] },
  ],
})

function renderMail() {
  return render(
    <MemoryRouter initialEntries={['/w/connect/agents/echo/mail']}>
      <Routes><Route path="/w/:workspace/agents/:slug/mail" element={<AgentMailSection />} /></Routes>
    </MemoryRouter>,
  )
}

describe('AgentMailSection', () => {
  it('lists threads and loads one on click, in order with direction labels', async () => {
    fetchMailThreads.mockResolvedValue([thread])
    fetchMailThread.mockResolvedValue(detail())
    renderMail()
    const row = await screen.findByText('Grant question')
    expect(fetchMailThreads).toHaveBeenCalledWith('connect', 'echo')
    fireEvent.click(row)
    await waitFor(() => expect(fetchMailThread).toHaveBeenCalledWith('connect', 't1'))
    const first = await screen.findByText('First hello')
    const second = screen.getByText('Second reply')
    expect(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByText('in')).toBeTruthy()
    expect(screen.getByText('out')).toBeTruthy()
    expect(screen.getByText('delivered')).toBeTruthy()
  })

  it('renders html only in an iframe with an empty sandbox', async () => {
    fetchMailThreads.mockResolvedValue([thread])
    fetchMailThread.mockResolvedValue(detail('<p>Hi</p>'))
    const { container } = renderMail()
    fireEvent.click(await screen.findByText('Grant question'))
    await waitFor(() => expect(container.querySelector('iframe')).toBeTruthy())
    const frame = container.querySelector('iframe')!
    expect(frame.getAttribute('sandbox')).toBe('')
    expect(frame.getAttribute('srcdoc')).toContain('<p>Hi</p>')
  })

  it('shows attachments as download links', async () => {
    fetchMailThreads.mockResolvedValue([thread])
    fetchMailThread.mockResolvedValue(detail())
    renderMail()
    fireEvent.click(await screen.findByText('Grant question'))
    const link = await screen.findByRole('link', { name: /budget\.pdf/ })
    expect(link.getAttribute('href')).toBe('https://s3/budget?sig=1')
  })

  it('explains an agent with no mail and links to email settings', async () => {
    fetchMailThreads.mockResolvedValue([])
    renderMail()
    expect(await screen.findByText(/This agent has no email address/)).toBeTruthy()
    const link = screen.getByRole('link', { name: /Settings → Email/ })
    expect(link.getAttribute('href')).toBe('/w/connect/settings/email')
  })
})
