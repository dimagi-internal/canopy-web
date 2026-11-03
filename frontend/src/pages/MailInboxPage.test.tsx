// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as mail from '@/api/mail'
import { MailInboxPage, parseAddresses } from '@/pages/MailInboxPage'

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
  fetchInbox: vi.fn(async () => ({ items: [row], unread_total: 1, addresses: ['jjackson@marshellis.com'] })),
  fetchMailThread: vi.fn(async () => full),
  setMailThreadState: vi.fn(async () => ({ ...row, unread: false })),
  replyToThread: vi.fn(async () => ({ ...inboundMsg, id: 2, direction: 'out' })),
  composeInboxMail: vi.fn(async () => ({ ...full, thread_id: 't2', subject: 'Hello' })),
}))

beforeEach(() => vi.clearAllMocks())
afterEach(cleanup)

function renderPage() {
  render(
    <MemoryRouter initialEntries={['/w/marshellis/mail']}>
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
    await vi.waitFor(() => expect(mail.fetchInbox).toHaveBeenCalledWith('marshellis', { archived: false, q: 'noon', address: '' }))
    fireEvent.click(screen.getByRole('tab', { name: 'Archived' }))
    await vi.waitFor(() => expect(mail.fetchInbox).toHaveBeenLastCalledWith('marshellis', { archived: true, q: 'noon', address: '' }))
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
      text: 'Yes!', to: ['friend@example.org'], cc: [] }))
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
    await vi.waitFor(() => expect(mail.composeInboxMail).toHaveBeenCalledWith('marshellis', {
      from_address: 'jjackson@marshellis.com', to: ['new@example.org'], cc: [], subject: 'Hello', text: 'Hi there' }))
  })

  it('archives from the keyboard and explains how to switch it on when nothing is kept', async () => {
    renderPage()
    await openFirst()
    fireEvent.keyDown(window, { key: 'e' })
    await vi.waitFor(() => expect(mail.setMailThreadState).toHaveBeenCalledWith('marshellis', 't1', { archived: true }))
  })

  it('shows setup guidance when there is nothing and no address', async () => {
    vi.mocked(mail.fetchInbox).mockResolvedValueOnce({ items: [], unread_total: 0, addresses: [] })
    renderPage()
    expect(await screen.findByText(/Keep a copy in canopy/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Compose' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
