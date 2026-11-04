// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as mail from '@/api/mail'
import { invalidAddresses, parseAddresses } from '@/components/mail/addresses'
import { mailTiming } from '@/components/mail/MailClient'
import { hasRemoteImages, sandboxedSrcDoc, senderWarning, splitQuoted } from '@/components/mail/MessageView'
import { MailInboxPage } from '@/pages/MailInboxPage'

const inboundMsg = {
  id: 1, direction: 'in', message_id: '<1@x>', from_addr: 'friend@example.org', from_name: 'Friend',
  to: ['jjackson@marshellis.com', 'pal@example.org'], cc: [], reply_to: [], subject: 'Lunch?', text: 'Noon on Friday?',
  html: '', delivery_status: '', verdicts: {}, created_at: '2026-11-02T20:00:00Z', attachments: [],
}
const row = {
  thread_id: 't1', address: 'jjackson@marshellis.com', agent: '', subject: 'Lunch?', message_count: 1, unread: true,
  archived: false, last_message_at: '2026-11-02T20:00:00Z', forwarded: true, counterpart: 'friend@example.org',
  default_reply_to: [], messages: [],
}
const full = { ...row, default_reply_to: ['friend@example.org'], messages: [inboundMsg] }

vi.mock('@/api/mail', async (orig) => ({
  ...(await orig<typeof import('@/api/mail')>()),
  fetchInbox: vi.fn(async () => ({ items: [row], unread_total: 1, addresses: ['jjackson@marshellis.com'], agent_address: '', has_more: false })),
  fetchMailThread: vi.fn(async () => full),
  setMailThreadState: vi.fn(async () => ({ ...row, unread: false })),
  replyToThread: vi.fn(async () => ({ ...inboundMsg, id: 2, direction: 'out' })),
  sendMail: vi.fn(async () => ({ ...inboundMsg, id: 3, direction: 'out', thread_id: 't2' })),
  fetchMailDrafts: vi.fn(async () => []),
  saveMailDraft: vi.fn(async (_w: string, id: string) => ({ id, agent: '', thread_id: '', from_address: '', to: '', cc: '', bcc: '', subject: '', text: '', uploads: [], updated_at: '2026-11-02T20:00:00Z' })),
  deleteMailDraft: vi.fn(async () => undefined),
  deleteMailThread: vi.fn(async () => undefined),
  emptyMailFolder: vi.fn(async () => undefined),
}))

beforeEach(() => {
  vi.clearAllMocks()
  mailTiming.sendDelayMs = 0
  mailTiming.saveDelayMs = 0
  mailTiming.readDelayMs = 0
})
afterEach(cleanup)

function renderPage(url = '/w/marshellis/mail') {
  render(
    <MemoryRouter initialEntries={[url]}>
      <Routes><Route path="/w/:workspace/mail" element={<MailInboxPage />} /></Routes>
    </MemoryRouter>,
  )
}

async function openFirst() {
  fireEvent.click(await screen.findByRole('button', { name: /Lunch\?/ }))
  await screen.findByText('Noon on Friday?')
}

describe('parseAddresses', () => {
  it('takes bare and named addresses, dedupes and lowercases', () => {
    expect(parseAddresses('A@x.org, B <b@y.org>; a@X.org\nnot-an-address')).toEqual(['a@x.org', 'b@y.org'])
  })
  it('does not split a quoted name, and reports what is not an address', () => {
    expect(parseAddresses('"Doe, Jane" <j@x.org>')).toEqual(['j@x.org'])
    expect(invalidAddresses('bob, priya@example.org, sam@')).toEqual(['bob', 'sam@'])
  })
})

describe('MailInboxPage', () => {
  it('lists the inbox with the unread count, opens a message and marks it read', async () => {
    renderPage()
    expect(await screen.findByRole('tab', { name: 'Inbox (1)' })).toBeTruthy()
    await openFirst()
    await vi.waitFor(() => expect(mail.setMailThreadState).toHaveBeenCalledWith('marshellis', 't1', { unread: false }))
  })

  it('searches and switches to Archived through the server', async () => {
    renderPage()
    await screen.findByRole('button', { name: /Lunch\?/ })
    fireEvent.change(screen.getByLabelText('Search mail'), { target: { value: 'noon' } })
    await vi.waitFor(() => expect(mail.fetchInbox).toHaveBeenCalledWith('marshellis', { forwarded: true }, { folder: 'inbox', q: 'noon', address: '', limit: 50 }))
    fireEvent.click(screen.getByRole('tab', { name: 'Archived' }))
    await vi.waitFor(() => expect(mail.fetchInbox).toHaveBeenLastCalledWith('marshellis', { forwarded: true }, { folder: 'archived', q: 'noon', address: '', limit: 50 }))
  })

  it('replies to the sender with the original quoted, from the thread', async () => {
    renderPage()
    await openFirst()
    fireEvent.click(screen.getByRole('button', { name: 'Reply' }))
    const form = screen.getByRole('form', { name: 'Reply' })
    expect((within(form).getByLabelText('To') as HTMLInputElement).value).toBe('friend@example.org')
    expect((within(form).getByLabelText('Message') as HTMLTextAreaElement).value).toContain('> Noon on Friday?')
    fireEvent.change(within(form).getByLabelText('Message'), { target: { value: 'Yes!' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Send' }))
    await vi.waitFor(() => expect(mail.replyToThread).toHaveBeenCalledWith('marshellis', 't1', {
      text: 'Yes!', to: ['friend@example.org'], cc: [], bcc: [], attachments: [] }))
  })

  it('reply all copies the other recipients but never my own address', async () => {
    renderPage()
    await openFirst()
    fireEvent.click(screen.getByRole('button', { name: 'Reply all' }))
    const form = screen.getByRole('form', { name: 'Reply' })
    expect((within(form).getByLabelText('Cc') as HTMLInputElement).value).toBe('pal@example.org')
  })

  it('composes a new message from one of my addresses', async () => {
    renderPage()
    await screen.findByRole('button', { name: /Lunch\?/ })
    fireEvent.click(screen.getByRole('button', { name: 'Compose' }))
    const form = await screen.findByRole('form', { name: 'New message' })
    fireEvent.change(within(form).getByLabelText('To'), { target: { value: 'new@example.org' } })
    fireEvent.change(within(form).getByLabelText('Subject'), { target: { value: 'Hello' } })
    fireEvent.change(within(form).getByLabelText('Message'), { target: { value: 'Hi there' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Send' }))
    await vi.waitFor(() => expect(mail.sendMail).toHaveBeenCalledWith('marshellis', {
      agent: undefined, from_address: 'jjackson@marshellis.com', to: ['new@example.org'], cc: [], bcc: [], attachments: [], subject: 'Hello', text: 'Hi there' }))
  })

  it('archives from the keyboard and explains how to switch it on when nothing is kept', async () => {
    renderPage()
    await openFirst()
    fireEvent.keyDown(window, { key: 'e' })
    await vi.waitFor(() => expect(mail.setMailThreadState).toHaveBeenCalledWith('marshellis', 't1', { archived: true }),
      { timeout: 5000 })
  })

  it('refuses to send when an address is not one, and says which', async () => {
    renderPage()
    await screen.findByRole('button', { name: /Lunch\?/ })
    fireEvent.click(screen.getByRole('button', { name: 'Compose' }))
    const form = await screen.findByRole('form', { name: 'New message' })
    fireEvent.change(within(form).getByLabelText('To'), { target: { value: 'bob, new@example.org' } })
    fireEvent.change(within(form).getByLabelText('Subject'), { target: { value: 'Hello' } })
    fireEvent.change(within(form).getByLabelText('Message'), { target: { value: 'Hi' } })
    expect(within(form).getByRole('alert').textContent).toContain('bob')
    expect((within(form).getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('keeps a draft when you click away, and opening a reply and typing nothing saves nothing', async () => {
    renderPage()
    await openFirst()
    fireEvent.click(screen.getByRole('button', { name: 'Reply' }))
    await new Promise((r) => setTimeout(r, 20))
    expect(mail.saveMailDraft).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'half a thought' } })
    fireEvent.keyDown(screen.getByLabelText('Message'), { key: 'Escape' })
    expect(screen.queryByRole('form', { name: 'Reply' })).toBeNull()
    await vi.waitFor(() => expect(mail.saveMailDraft).toHaveBeenCalledWith('marshellis', expect.any(String),
      expect.objectContaining({ text: 'half a thought', thread_id: 't1' })))
    expect(mail.deleteMailDraft).not.toHaveBeenCalled()
  })

  it('a send can be undone until its moment passes, and the words come back', async () => {
    mailTiming.sendDelayMs = 60_000
    renderPage()
    await openFirst()
    fireEvent.click(screen.getByRole('button', { name: 'Reply' }))
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Yes!' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Undo' }))
    expect((screen.getByLabelText('Message') as HTMLTextAreaElement).value).toBe('Yes!')
    expect(mail.replyToThread).not.toHaveBeenCalled()
  })

  it('opens the thread the URL names, and the folder too', async () => {
    renderPage('/w/marshellis/mail?folder=archived&thread=t1')
    await screen.findByText('Noon on Friday?')
    expect(mail.fetchMailThread).toHaveBeenCalledWith('marshellis', 't1')
    await vi.waitFor(() => expect(mail.fetchInbox).toHaveBeenCalledWith('marshellis', { forwarded: true },
      expect.objectContaining({ folder: 'archived' })))
  })

  it('moving to Trash offers Undo, and Undo puts it back', async () => {
    renderPage()
    await openFirst()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await vi.waitFor(() => expect(mail.setMailThreadState).toHaveBeenCalledWith('marshellis', 't1', { trashed: true }))
    fireEvent.click(await screen.findByRole('button', { name: 'Undo' }))
    await vi.waitFor(() => expect(mail.setMailThreadState).toHaveBeenCalledWith('marshellis', 't1', { trashed: false }))
  })

  it('selects several from the list and acts on them together', async () => {
    renderPage()
    fireEvent.click(await screen.findByLabelText('Select all'))
    fireEvent.click(screen.getByRole('button', { name: 'Spam' }))
    await vi.waitFor(() => expect(mail.setMailThreadState).toHaveBeenCalledWith('marshellis', 't1', { spam: true }))
  })

  it('fetches new mail by itself when the tab comes back', async () => {
    renderPage()
    await screen.findByRole('button', { name: /Lunch\?/ })
    const before = vi.mocked(mail.fetchInbox).mock.calls.length
    fireEvent.focus(window)
    await vi.waitFor(() => expect(vi.mocked(mail.fetchInbox).mock.calls.length).toBeGreaterThan(before))
  })

  it('shows setup guidance when there is nothing and no address', async () => {
    vi.mocked(mail.fetchInbox).mockResolvedValueOnce({ items: [], unread_total: 0, addresses: [], agent_address: '', has_more: false })
    renderPage()
    expect(await screen.findByText(/Mail forwarded to you is kept here/)).toBeTruthy()
  })
})

describe('rendering mail', () => {
  it('puts mail on a white page, CSP first', () => {
    const out = sandboxedSrcDoc('<p>Hi</p>')
    expect(out.startsWith('<meta http-equiv="Content-Security-Policy"')).toBe(true)
    expect(out).toContain('html{background:#fff')
    expect(out).toContain("img-src data:")
    expect(sandboxedSrcDoc('<p>Hi</p>', true)).toContain('img-src https: data:')
  })

  it('opens web links in a new tab and removes script ones', () => {
    const out = sandboxedSrcDoc('<a href="https://x.org">a</a><a href="javascript:alert(1)">b</a><base href="https://evil.example" target="_self">')
    expect(out).toContain('target="_blank"')
    expect(out).toContain('rel="noopener noreferrer"')
    expect(out).not.toContain('javascript:')
    expect(out).not.toContain('evil.example')
  })

  it('notices remote images, a failed sender check, and quoted text', () => {
    expect(hasRemoteImages('<img alt="x" src="https://t.example/p.gif">')).toBe(true)
    expect(hasRemoteImages('<img src="data:image/png;base64,AA">')).toBe(false)
    expect(senderWarning({ dmarc: 'FAIL', spf: 'PASS' })).toContain('DMARC')
    expect(senderWarning({ dmarc: 'PASS', spf: 'FAIL', dkim: 'PASS' })).toBeNull()
    expect(splitQuoted('Thanks!\n\nOn Tue, Jon wrote:\n> hello\n> there')).toEqual({ said: 'Thanks!', quoted: 'On Tue, Jon wrote:\n> hello\n> there' })
    expect(splitQuoted('> I disagree with this\nand here is why')).toEqual({ said: '> I disagree with this\nand here is why', quoted: '' })
  })
})
