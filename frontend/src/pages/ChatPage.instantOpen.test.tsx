// @vitest-environment jsdom
import { cleanup, render, renderHook, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * Opening a chat paints before its socket's snapshot lands: an ordinary chat a list
 * already named opens without the private gate's read, its transcript shows from
 * this tab's last copy or the REST tail, and a private one is never painted early.
 */

const prependMessages = vi.fn()
let connected = false
vi.mock('canopy-ui/chat', async (orig) => {
  const real = await orig<typeof import('canopy-ui/chat')>()
  return {
    ...real,
    ChatPanel: () => <div data-testid="chat-panel" />,
    useSessionSocket: () => ({
      state: { messages: [], activity: 'idle', menu: null, participants: [], presence_user_ids: [],
               current_user_id: 1, active_draft: null },
      connected, awaitingReply: false, sendChat: vi.fn(), stopChat: vi.fn(),
      unqueueChat: vi.fn(), updateDraft: vi.fn(), takeOverDraft: vi.fn(), discardDraft: vi.fn(),
      prependMessages, noteLocalSend: vi.fn(), typingVisibility: 'live', setTypingVisibility: vi.fn(),
    }),
  }
})

const TAIL = [{ turn_index: 3, role: 'user', content: {}, plaintext: 'hello', created_at: '2026-10-24T00:00:00Z' }]
const SESSION = {
  id: 's1', sensitive: false, my_role: 'owner', agent_slug: 'labsdata', runner_name: 'box',
  title: 'T', workspace: 'w1', messages: TAIL,
}

const getSession = vi.fn()
const listSlashCommands = vi.fn()
vi.mock('@/api/chat', async (orig) => {
  const real = await orig<typeof import('@/api/chat')>()
  return {
    ...real,
    getSession: (...a: unknown[]) => getSession(...a),
    listSlashCommands: (...a: unknown[]) => listSlashCommands(...a),
    attachSession: async () => undefined,
    detachSession: async () => undefined,
  }
})

const { ChatPage } = await import('./ChatPage')
const { forgetAll, rememberListed, rememberMessages, rememberMeta } = await import('./chat/sessionCache')
const { useSlashCommands } = await import('./chat/useSlashCommands')

beforeEach(() => {
  connected = false
  listSlashCommands.mockResolvedValue([])
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => [] }) as unknown as Response))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
  forgetAll()
})

const page = () => render(
  <MemoryRouter initialEntries={['/ide/s1']}>
    <Routes><Route path="/ide/:id" element={<ChatPage compact />} /></Routes>
  </MemoryRouter>,
)

describe('opening a chat', () => {
  it('opens one a list named as ordinary at once, and paints the REST tail before the snapshot', async () => {
    connected = true // the socket opens well before its snapshot lands
    rememberListed([{ id: 's1', sensitive: false }])
    getSession.mockResolvedValue(SESSION)
    page()
    expect(screen.getByTestId('chat-panel')).toBeTruthy()
    await waitFor(() => expect(prependMessages).toHaveBeenCalledTimes(1))
    expect(prependMessages.mock.calls[0][0]).toMatchObject([{ turn_index: 3, plaintext: 'hello' }])
    expect(getSession).toHaveBeenCalledTimes(1)
  })

  it("paints this tab's last copy of a chat straight away", () => {
    rememberMeta(SESSION as never)
    rememberMessages('s1', [{ id: 't3', turn_index: 3 }] as never)
    getSession.mockReturnValue(new Promise(() => {}))
    page()
    expect(screen.getByTestId('chat-panel')).toBeTruthy()
    expect(prependMessages).toHaveBeenCalledWith([{ id: 't3', turn_index: 3 }])
  })

  it('never paints a private conversation early', async () => {
    sessionStorage.setItem('canopy.private-open.s1', '1')
    getSession.mockResolvedValue({ ...SESSION, sensitive: true, you_are_in_it: true, sealed_key: null })
    page()
    await screen.findByTestId('chat-panel')
    await waitFor(() => expect(getSession).toHaveBeenCalled())
    expect(prependMessages).not.toHaveBeenCalled()
    sessionStorage.clear()
  })
})

describe('useSlashCommands', () => {
  // Its own agent: the list is kept for the life of the tab, and the page tests above
  // already read one for SESSION's.
  const SESSION = { id: 's1', agent_slug: 'ace', runner_name: 'box', my_role: 'owner' }
  it('waits for the detail, then reads the list once per agent, runner and role', async () => {
    listSlashCommands.mockResolvedValue([{ name: 'labsdata:report', description: '', args_hint: '', source: 'agent' }])
    const first = renderHook(({ meta, k }) => useSlashCommands('s1', meta as never, k),
      { initialProps: { meta: null as unknown, k: 'a' } })
    expect(listSlashCommands).not.toHaveBeenCalled()
    first.rerender({ meta: SESSION, k: 'a' })
    first.rerender({ meta: SESSION, k: 'b' })
    await waitFor(() => expect(first.result.current).toHaveLength(1))
    const second = renderHook(() => useSlashCommands('s2', { ...SESSION, id: 's2' } as never, 'a'))
    await waitFor(() => expect(second.result.current).toHaveLength(1))
    expect(listSlashCommands).toHaveBeenCalledTimes(1)
  })

  it('asks again for a chat with no runner yet, whose list changes on its first claim', async () => {
    listSlashCommands.mockResolvedValue([])
    const unbound = { ...SESSION, runner_name: null }
    const h = renderHook(({ k }) => useSlashCommands('s1', unbound as never, k), { initialProps: { k: '|false' } })
    await waitFor(() => expect(listSlashCommands).toHaveBeenCalledTimes(1))
    h.rerender({ k: 'box|true' })
    await waitFor(() => expect(listSlashCommands).toHaveBeenCalledTimes(2))
  })
})
