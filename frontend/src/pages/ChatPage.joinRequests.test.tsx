// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * A request to join a private conversation, IN canopy's chat page (and so /ide's
 * pane, which is this page): read on open, kept live by the socket's
 * `session.join_requests` frame, answered inline.
 */

let socketOpts: { onUnknownEvent?: (f: { event: string; data?: unknown }) => void } = {}
vi.mock('canopy-ui/chat', async (orig) => {
  const real = await orig<typeof import('canopy-ui/chat')>()
  return {
    ...real,
    ChatPanel: () => <div data-testid="chat-panel" />,
    useSessionSocket: (opts: typeof socketOpts) => {
      socketOpts = opts
      return {
        state: { messages: [], activity: 'idle', menu: null, participants: [], presence_user_ids: [],
                 current_user_id: 1, active_draft: null },
        connected: true, awaitingReply: false, sendChat: vi.fn(), stopChat: vi.fn(),
        unqueueChat: vi.fn(), updateDraft: vi.fn(), takeOverDraft: vi.fn(), discardDraft: vi.fn(),
        prependMessages: vi.fn(), noteLocalSend: vi.fn(), typingVisibility: 'live',
        setTypingVisibility: vi.fn(),
      }
    },
  }
})

const SESSION = {
  id: 's1', sensitive: true, you_are_in_it: true, my_role: 'owner', sealed_key: null,
  agent_slug: 'labsdata', title: '', workspace: 'w1', created_by: 'jj@dimagi.com',
  started_by_name: 'Jonathan Jackson', your_join_request: '',
}
const ADA = {
  id: 'r1', session_id: 's1', requested_by: 'ada@dimagi.com', requested_by_name: 'Ada Lovelace',
  status: 'pending', decided_by: '', decided_at: null, created_at: '2026-10-23T12:00:00Z',
}

const getSession = vi.fn()
const listJoinRequests = vi.fn()
const declineJoinRequest = vi.fn()
const addParticipant = vi.fn()
vi.mock('@/api/chat', async (orig) => {
  const real = await orig<typeof import('@/api/chat')>()
  return {
    ...real,
    getSession: (...a: unknown[]) => getSession(...a),
    listJoinRequests: (...a: unknown[]) => listJoinRequests(...a),
    declineJoinRequest: (...a: unknown[]) => declineJoinRequest(...a),
    addParticipant: (...a: unknown[]) => addParticipant(...a),
    attachSession: async () => undefined,
    detachSession: async () => undefined,
  }
})

const { ChatPage } = await import('./ChatPage')

beforeEach(() => {
  sessionStorage.setItem('canopy.private-open.s1', '1')
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => [] }) as unknown as Response))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
  sessionStorage.clear()
})

const page = () => render(
  <MemoryRouter initialEntries={['/w/w1/sessions/s1']}>
    <Routes><Route path="/w/:workspace/sessions/:id" element={<ChatPage />} /></Routes>
  </MemoryRouter>,
)

describe('a request to join, in the chat page', () => {
  it('shows its owner who asked, inline, with Add them and Decline', async () => {
    getSession.mockResolvedValue(SESSION)
    listJoinRequests.mockResolvedValue([ADA])
    page()
    const row = await screen.findByTestId('join-request')
    expect(row.textContent).toContain('Ada Lovelace asked to join this conversation')
    expect(listJoinRequests).toHaveBeenCalledWith('s1')
    declineJoinRequest.mockResolvedValue({ ...ADA, status: 'declined' })
    fireEvent.click(screen.getByTestId('join-decline-r1'))
    await waitFor(() => expect(screen.queryByTestId('join-request')).toBeNull())
    expect(declineJoinRequest).toHaveBeenCalledWith('r1')
  })

  it('Add them adds them through the participants flow', async () => {
    getSession.mockResolvedValue(SESSION)
    listJoinRequests.mockResolvedValue([ADA])
    addParticipant.mockResolvedValue([])
    page()
    fireEvent.click(await screen.findByTestId('join-add-r1'))
    await waitFor(() => expect(addParticipant).toHaveBeenCalledWith('s1', 'ada@dimagi.com', 'editor'))
    await waitFor(() => expect(screen.queryByTestId('join-request')).toBeNull())
  })

  it('arrives live on the socket, without a reload', async () => {
    getSession.mockResolvedValue(SESSION)
    listJoinRequests.mockResolvedValue([])
    page()
    await waitFor(() => expect(listJoinRequests).toHaveBeenCalled())
    expect(screen.queryByTestId('join-request')).toBeNull()
    act(() => socketOpts.onUnknownEvent?.({ event: 'session.join_requests', data: { requests: [ADA] } }))
    expect((await screen.findByTestId('join-request')).textContent).toContain('Ada Lovelace')
    act(() => socketOpts.onUnknownEvent?.({ event: 'session.join_requests', data: { requests: [] } }))
    await waitFor(() => expect(screen.queryByTestId('join-request')).toBeNull())
  })

  it('someone else in it sees the request, but only its owner answers it', async () => {
    getSession.mockResolvedValue({ ...SESSION, my_role: 'editor' })
    listJoinRequests.mockResolvedValue([ADA])
    page()
    expect((await screen.findByTestId('join-request')).textContent).toContain('its owner can add them')
    expect(screen.queryByTestId('join-add-r1')).toBeNull()
    expect(screen.queryByTestId('join-decline-r1')).toBeNull()
  })
})
