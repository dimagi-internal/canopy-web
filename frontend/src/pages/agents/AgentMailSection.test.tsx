// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const { fetchMailThreads, fetchMailThread } = vi.hoisted(() => ({
  fetchMailThreads: vi.fn(),
  fetchMailThread: vi.fn(),
}))
vi.mock('@/api/mail', () => ({ fetchMailThreads, fetchMailThread }))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({ agent: { slug: 'echo', name: 'Echo', workspace: 'connect' } }),
}))

const { AgentMailSection, sandboxedSrcDoc } = await import('./AgentMailSection')

afterEach(() => { cleanup(); vi.clearAllMocks() })

const thread = {
  thread_id: 't1', address: 'echo@mail.example.org', agent: 'echo', subject: 'Grant question',
  counterpart: 'amy@x.org', message_count: 2, unread: true, archived: false, last_message_at: '2026-10-11T10:00:00Z',
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
    fetchMailThreads.mockResolvedValue({ items: [thread], agent_address: 'echo@mail.example.org' })
    fetchMailThread.mockResolvedValue(detail())
    renderMail()
    const row = await screen.findByText('Grant question')
    expect(fetchMailThreads).toHaveBeenCalledWith('connect', 'echo')
    expect(screen.getByRole('columnheader', { name: 'With' })).toBeTruthy()
    expect(screen.getAllByText('amy@x.org').length).toBeGreaterThan(0)
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
    fetchMailThreads.mockResolvedValue({ items: [thread], agent_address: 'echo@mail.example.org' })
    fetchMailThread.mockResolvedValue(detail('<p>Hi</p>'))
    const { container } = renderMail()
    fireEvent.click(await screen.findByText('Grant question'))
    await waitFor(() => expect(container.querySelector('iframe')).toBeTruthy())
    const frame = container.querySelector('iframe')!
    expect(frame.getAttribute('sandbox')).toBe('')
    expect(frame.getAttribute('srcdoc')).toContain('<p>Hi</p>')
    expect(frame.getAttribute('srcdoc')!.startsWith(
      `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">`,
    )).toBe(true)
    expect(frame.getAttribute('referrerpolicy')).toBe('no-referrer')
  })

  it.each([
    '<meta http-equiv="refresh" content="0;url=https://evil.example">',
    '<META HTTP-EQUIV="Refresh" CONTENT="0; URL=https://evil.example">',
    '<meta content="0;url=https://evil.example" http-equiv=refresh>',
    "<meta name=x content='0;url=https://evil.example' http-equiv = ' REFRESH ' />",
  ])('strips a meta refresh before building srcDoc: %s', (meta) => {
    const out = sandboxedSrcDoc(`<html><head>${meta}</head><body><p>Hi</p></body></html>`)
    expect(out.toLowerCase()).not.toContain('refresh')
    expect(out).not.toContain('evil.example')
    expect(out).toContain('<p>Hi</p>')
    expect(out.startsWith(
      `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">`,
    )).toBe(true)
  })

  it('leaves html without a meta refresh untouched', () => {
    expect(sandboxedSrcDoc('<p>Hi</p>').endsWith('<p>Hi</p>')).toBe(true)
  })

  it('shows attachments as download links', async () => {
    fetchMailThreads.mockResolvedValue({ items: [thread], agent_address: 'echo@mail.example.org' })
    fetchMailThread.mockResolvedValue(detail())
    renderMail()
    fireEvent.click(await screen.findByText('Grant question'))
    const link = await screen.findByRole('link', { name: /budget\.pdf/ })
    expect(link.getAttribute('href')).toBe('https://s3/budget?sig=1')
  })

  it('explains an agent with no address and links to email settings', async () => {
    fetchMailThreads.mockResolvedValue({ items: [], agent_address: '' })
    renderMail()
    expect(await screen.findByText(/This agent has no email address/)).toBeTruthy()
    const link = screen.getByRole('link', { name: /Settings → Email/ })
    expect(link.getAttribute('href')).toBe('/w/connect/settings/email')
  })

  it('says no mail yet when the agent has an address but no threads', async () => {
    fetchMailThreads.mockResolvedValue({ items: [], agent_address: 'echo@mail.example.org' })
    renderMail()
    expect(await screen.findByText('No mail yet for echo@mail.example.org.')).toBeTruthy()
    expect(screen.queryByText(/no email address/)).toBeNull()
  })

  it('shows an error, not the no-address state, when the fetch fails', async () => {
    fetchMailThreads.mockRejectedValue(new Error('boom'))
    renderMail()
    expect((await screen.findByRole('alert')).textContent).toBe('boom')
    expect(screen.queryByText(/no email address/)).toBeNull()
  })

  it('shows an alert when the thread fails to load', async () => {
    fetchMailThreads.mockResolvedValue({ items: [thread], agent_address: 'echo@mail.example.org' })
    fetchMailThread.mockRejectedValue(new Error('thread gone'))
    renderMail()
    fireEvent.click(await screen.findByText('Grant question'))
    expect((await screen.findByRole('alert')).textContent).toBe('thread gone')
  })

  it('shows the LAST clicked thread when responses arrive out of order', async () => {
    const t2 = { ...thread, thread_id: 't2', subject: 'Second thread' }
    fetchMailThreads.mockResolvedValue({ items: [thread, t2], agent_address: 'a@b.org' })
    let res1: (v: unknown) => void = () => {}
    let res2: (v: unknown) => void = () => {}
    fetchMailThread.mockImplementation((_ws: string, id: string) =>
      new Promise((r) => { if (id === 't1') res1 = r; else res2 = r }))
    renderMail()
    fireEvent.click(await screen.findByText('Grant question'))
    fireEvent.click(screen.getByText('Second thread'))
    res2({ ...detail(), ...t2, messages: [{ ...detail().messages[1], text: 'from two' }] })
    res1(detail())
    expect(await screen.findByText('from two')).toBeTruthy()
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByText('First hello')).toBeNull()
  })

  it('renders cc, and non-https attachment urls as plain text', async () => {
    fetchMailThreads.mockResolvedValue({ items: [thread], agent_address: 'a@b.org' })
    const d = detail()
    fetchMailThread.mockResolvedValue({ ...d, messages: [{ ...d.messages[0], cc: ['bob@x.org'],
      attachments: [{ filename: 'evil.txt', content_type: 'text/plain', size: 1, url: 'javascript:alert(1)' }] }] })
    renderMail()
    fireEvent.click(await screen.findByText('Grant question'))
    expect(await screen.findByText('cc bob@x.org')).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'evil.txt' })).toBeNull()
    expect(screen.getByText('evil.txt')).toBeTruthy()
  })
})
