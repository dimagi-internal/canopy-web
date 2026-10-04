// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ChatSession } from '@/api/chat'

const listSessions = vi.fn()
const sendMessage = vi.fn()
const closeSession = vi.fn()
vi.mock('@/api/chat', () => ({
  listSessions: (...a: unknown[]) => listSessions(...a),
  sendMessage: (...a: unknown[]) => sendMessage(...a),
  closeSession: (...a: unknown[]) => closeSession(...a),
}))

import { SessionFeed } from './SessionFeed'

const s = (id: string, fields: Partial<ChatSession> = {}): ChatSession =>
  ({
    id,
    title: `title ${id}`,
    workspace: 'dimagi',
    agent_slug: 'hal',
    project: '',
    running: false,
    waiting_on_you: false,
    agent_spoke_last: true,
    last_reply: `**Reply** from ${id}`,
    last_activity_at: '2026-10-03T10:00:00Z',
    runner_online: true,
    runner_status: 'online',
    runner_name: 'jj-mbp',
    ...fields,
  }) as unknown as ChatSession

const renderFeed = () =>
  render(
    <MemoryRouter>
      <SessionFeed agents={[{ slug: 'hal', name: 'Hal' } as never]} />
    </MemoryRouter>,
  )

beforeEach(() => {
  vi.stubGlobal('crypto', { randomUUID: () => 'client-1' })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('SessionFeed', () => {
  it('asks the list for replies, and shows only sessions waiting on you', async () => {
    listSessions.mockResolvedValue([s('done'), s('busy', { running: true }), s('answered', { agent_spoke_last: false })])
    renderFeed()
    expect(await screen.findByTestId('feed-card-done')).toBeTruthy()
    expect(listSessions).toHaveBeenCalledWith('active', { reply: true })
    expect(screen.queryByTestId('feed-card-busy')).toBeNull()
    expect(screen.queryByTestId('feed-card-answered')).toBeNull()
    // The reply is rendered as markdown, not shown raw.
    expect(screen.getByText('Reply').tagName).toBe('STRONG')
    expect(screen.getByText(/with Hal · jj-mbp/)).toBeTruthy()
  })

  it('sends the next prompt in place and drops the card', async () => {
    listSessions.mockResolvedValue([s('a')])
    sendMessage.mockResolvedValue({ turn_id: 't1' })
    renderFeed()
    const box = await screen.findByLabelText('Reply to title a')
    fireEvent.change(box, { target: { value: 'ship it' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(screen.queryByTestId('feed-card-a')).toBeNull())
    expect(sendMessage).toHaveBeenCalledWith('a', 'ship it', 'client-1')
  })

  // Close must END the session, not archive it: an archived runner session is
  // un-archived by the runner's next report while its emdash task is still open,
  // so the card came straight back.
  it('Close closes the session (not archive) and drops the card', async () => {
    listSessions.mockResolvedValue([s('a', { status: 'active' })])
    closeSession.mockResolvedValue({ ok: true, closing: true, reason: '' })
    renderFeed()
    fireEvent.click(await screen.findByTestId('feed-done-a'))
    await waitFor(() => expect(screen.queryByTestId('feed-card-a')).toBeNull())
    expect(closeSession).toHaveBeenCalledWith('a')
    expect(await screen.findByTestId('feed-empty')).toBeTruthy()
  })

  it('a refused close keeps the card and says why', async () => {
    listSessions.mockResolvedValue([s('a', { status: 'active' })])
    closeSession.mockResolvedValue({ ok: false, closing: false, reason: 'unavailable' })
    renderFeed()
    fireEvent.click(await screen.findByTestId('feed-done-a'))
    expect(await screen.findByText('Could not close — jj-mbp is online')).toBeTruthy()
    expect(screen.getByTestId('feed-card-a')).toBeTruthy()
  })

  it('a failed send keeps the card and the draft, and says why', async () => {
    listSessions.mockResolvedValue([s('a')])
    sendMessage.mockRejectedValue(new Error('runner offline'))
    renderFeed()
    const box = (await screen.findByLabelText('Reply to title a')) as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'ship it' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByText('runner offline')).toBeTruthy()
    expect(screen.getByTestId('feed-card-a')).toBeTruthy()
    expect(box.value).toBe('ship it')
  })

  it('flags a session blocked on a dialog and points at the chat to answer it', async () => {
    listSessions.mockResolvedValue([s('q', { waiting_on_you: true, running: true })])
    renderFeed()
    expect(await screen.findByText('needs an answer')).toBeTruthy()
    expect(screen.getByRole('link', { name: /Answer it in the chat/ }).getAttribute('href')).toBe('/w/dimagi/chat/q')
  })

  it('a short single-workspace feed shows no chips and no workspace names', async () => {
    listSessions.mockResolvedValue([s('a'), s('b')])
    renderFeed()
    await screen.findByTestId('feed-card-a')
    expect(screen.queryByTestId('feed-chips')).toBeNull()
    expect(screen.queryByText(/· dimagi/)).toBeNull()
  })

  it('a dense cross-workspace feed gets agent chips and names each workspace', async () => {
    listSessions.mockResolvedValue([
      s('h1'), s('h2'),
      s('a1', { agent_slug: 'ace', workspace: 'connect' }),
      s('p1', { agent_slug: null, project: 'canopy-web' }),
    ])
    renderFeed()
    const chips = await screen.findByTestId('feed-chips')
    expect(chips.textContent).toContain('All 4')
    expect(chips.textContent).toContain('Hal 2')
    expect(screen.getAllByText(/· connect/).length).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: /^Hal/ }))
    expect(screen.queryByTestId('feed-card-a1')).toBeNull()
    expect(screen.getByTestId('feed-card-h1')).toBeTruthy()
  })

  it('counts what it holds back on offline runners', async () => {
    listSessions.mockResolvedValue([s('a'), s('dead', { runner_online: false, runner_status: 'stale' })])
    renderFeed()
    expect((await screen.findByTestId('feed-parked')).textContent).toContain('1 more')
  })
})
