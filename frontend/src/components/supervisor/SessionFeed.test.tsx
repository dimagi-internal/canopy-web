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

// The New chat menu loads projects on mount; nothing here exercises it.
vi.mock('@/api/projects', () => ({ projectsApi: { listSlugs: () => Promise.resolve([]) } }))

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
    status: 'active',
    runner_online: true,
    runner_status: 'online',
    runner_name: 'jj-mbp',
    feed_status: 'waiting',
    ...fields,
  }) as unknown as ChatSession

const renderFeed = () =>
  render(
    <MemoryRouter>
      <SessionFeed agents={[{ slug: 'hal', name: 'Hal' } as never]} />
    </MemoryRouter>,
  )

// jsdom here exposes a `localStorage` whose methods are missing (see
// NoteComposer.test.tsx), so a plain map stands in, fresh for every test.
let store: Map<string, string>

beforeEach(() => {
  vi.stubGlobal('crypto', { randomUUID: () => 'client-1' })
  store = new Map()
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('SessionFeed', () => {
  it('asks the list for replies, and shows only sessions waiting on you', async () => {
    listSessions.mockResolvedValue([s('done'), s('busy', { running: true, feed_status: '' }), s('answered', { agent_spoke_last: false, feed_status: '' })])
    renderFeed()
    expect(await screen.findByTestId('feed-card-done')).toBeTruthy()
    expect(listSessions).toHaveBeenCalledWith('active', { reply: true })
    expect(screen.queryByTestId('feed-card-busy')).toBeNull()
    expect(screen.queryByTestId('feed-card-answered')).toBeNull()
    // The reply is rendered as markdown, not shown raw.
    expect(screen.getByText('Reply').tagName).toBe('STRONG')
    // The agent leads the card, ahead of the title, with the runner beside it.
    expect(screen.getByTestId('feed-source-done').textContent).toBe('Hal')
    expect(screen.getByText(/^jj-mbp ·/)).toBeTruthy()
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
  it('Close closes the session, stays marked until emdash confirms, then leaves', async () => {
    // Still listed after the relay (the runner has not reported yet), then gone —
    // the runner deleted the emdash task.
    listSessions
      .mockResolvedValueOnce([s('a', { status: 'active' })])
      .mockResolvedValueOnce([s('a', { status: 'active' })])
      .mockResolvedValue([])
    closeSession.mockResolvedValue({ ok: true, closing: true, reason: '' })
    renderFeed()
    fireEvent.click(await screen.findByTestId('feed-done-a'))
    expect(closeSession).toHaveBeenCalledWith('a')
    // Not hidden on faith: it stays, marked, until the runner confirms.
    expect((await screen.findByTestId('feed-done-a')).textContent).toBe('Closing in emdash…')
    await waitFor(() => expect(screen.queryByTestId('feed-card-a')).toBeNull(), { timeout: 5000 })
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
    expect(screen.getAllByText(/^connect(?: ·|$)/).length).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: /^Hal/ }))
    expect(screen.queryByTestId('feed-card-a1')).toBeNull()
    expect(screen.getByTestId('feed-card-h1')).toBeTruthy()
  })

  it('counts what it holds back on offline runners', async () => {
    listSessions.mockResolvedValue([s('a'), s('dead', { runner_online: false, runner_status: 'stale', feed_status: 'parked' })])
    renderFeed()
    expect((await screen.findByTestId('feed-parked')).textContent).toContain('1 more')
  })

  it('offers New chat right on the feed', async () => {
    listSessions.mockResolvedValue([])
    renderFeed()
    expect(await screen.findByTestId('feed-empty')).toBeTruthy()
    expect(screen.getByTestId('new-chat').textContent).toContain('New chat')
  })

  it('holds back sessions an agent ran on its own until you show them', async () => {
    listSessions.mockResolvedValue([
      s('chat', { turn_mode: 'auto', turn_origin: 'canopy_web_chat' }),
      s('cron', { turn_mode: 'auto', turn_origin: 'canopy_scheduler', feed_status: 'auto' }),
      s('mail', { turn_mode: 'manual', turn_origin: 'email' }),
    ])
    renderFeed()
    // A chat you are having stays even when its turn ran auto.
    expect(await screen.findByTestId('feed-card-chat')).toBeTruthy()
    expect(screen.getByTestId('feed-card-mail')).toBeTruthy()
    expect(screen.queryByTestId('feed-card-cron')).toBeNull()
    expect(screen.getByText('Show auto sessions (1)')).toBeTruthy()

    fireEvent.click(screen.getByTestId('feed-show-auto'))
    expect(screen.getByTestId('feed-card-cron')).toBeTruthy()
    // Remembered for this viewer.
    expect(store.get('canopy.supervisor.feed.showAuto')).toBe('1')
  })

  it('says when the only sessions waiting are hidden auto ones', async () => {
    listSessions.mockResolvedValue([s('cron', { turn_mode: 'auto', turn_origin: 'email', feed_status: 'auto' })])
    renderFeed()
    expect(await screen.findByTestId('feed-empty')).toBeTruthy()
    expect(screen.getByText(/1 session an agent ran on its own is hidden/)).toBeTruthy()
  })
})
