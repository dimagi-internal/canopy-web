// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as mail from '@/api/mail'
import { MailInboxPage } from '@/pages/MailInboxPage'

const thread = {
  thread_id: 't1', address: 'jjackson@marshellis.com', agent: '', subject: 'Lunch?', message_count: 1,
  unread: true, archived: false, last_message_at: '2026-11-02T20:00:00Z', forwarded: true,
  counterpart: 'friend@example.org', default_reply_to: [], messages: [],
}

vi.mock('@/api/mail', async (orig) => ({
  ...(await orig<typeof import('@/api/mail')>()),
  fetchMailThreads: vi.fn(async () => ({ items: [thread], agent_address: '' })),
  fetchMailThread: vi.fn(async () => ({ ...thread, messages: [{
    id: 1, direction: 'in', message_id: '<1@x>', from_addr: 'friend@example.org', from_name: '', to: ['jjackson@marshellis.com'],
    cc: [], reply_to: [], subject: 'Lunch?', text: 'Noon on Friday?', html: '', delivery_status: '', verdicts: {},
    created_at: '2026-11-02T20:00:00Z', attachments: [] }] })),
  setMailThreadState: vi.fn(async () => ({ ...thread, unread: false })),
}))

afterEach(cleanup)

function renderPage() {
  render(
    <MemoryRouter initialEntries={['/w/marshellis/mail']}>
      <Routes><Route path="/w/:workspace/mail" element={<MailInboxPage />} /></Routes>
    </MemoryRouter>,
  )
}

describe('MailInboxPage', () => {
  it('asks for the caller\'s own forwarded mail, opens a message and marks it read', async () => {
    renderPage()
    expect(mail.fetchMailThreads).toHaveBeenCalledWith('marshellis', undefined, true)
    fireEvent.click(await screen.findByRole('button', { name: 'Lunch?' }))
    expect(await screen.findByText('Noon on Friday?')).toBeTruthy()
    await vi.waitFor(() => expect(mail.setMailThreadState).toHaveBeenCalledWith('marshellis', 't1', { unread: false }))
  })
  it('explains how to turn it on when empty', async () => {
    vi.mocked(mail.fetchMailThreads).mockResolvedValueOnce({ items: [], agent_address: '' })
    renderPage()
    expect(await screen.findByText(/Keep a copy in their canopy inbox/)).toBeTruthy()
  })
})
