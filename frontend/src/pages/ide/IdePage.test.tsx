// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

import { IdePage } from './IdePage'

const chat = vi.hoisted(() => ({ listSessions: vi.fn(), closeSession: vi.fn() }))
const harness = vi.hoisted(() => ({ listRunners: vi.fn() }))
const agents = vi.hoisted(() => ({ listMyAgents: vi.fn() }))

vi.mock('@/api/chat', async (orig) => ({ ...(await orig<object>()), ...chat }))
vi.mock('@/api/harness', async (orig) => ({ ...(await orig<object>()), ...harness }))
vi.mock('@/api/agents', async (orig) => ({ ...(await orig<object>()), ...agents }))
vi.mock('@/hooks/useReconnectingSocket', () => ({
  useReconnectingSocket: () => ({ connected: false }),
}))
vi.mock('./useCloseReview', () => ({ useCloseReview: () => {} }))
// The pane stands in for the real chat; its two ways of closing are what /ide
// hands it. useSessionActions has its own tests for when each is called.
vi.mock('@/pages/ChatPage', () => ({
  ChatPage: ({ onLeave }: { onLeave: (relayed: boolean) => void }) => (
    <div data-testid="pane">
      <button type="button" onClick={() => onLeave(true)}>pane-relayed</button>
      <button type="button" onClick={() => onLeave(false)}>pane-closed</button>
    </div>
  ),
}))

const session = (id: string, minutesAgo: number, project = 'repo') => ({
  id,
  title: id,
  status: 'active',
  workspace: 'w',
  emdash_project: project,
  runner_name: 'box',
  runner_online: true,
  runner_status: 'online',
  runner_requirements: [],
  last_activity_at: new Date(Date.now() - minutesAgo * 60_000).toISOString(),
})

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>
}

async function open(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/ide" element={<IdePage />} />
        <Route path="/ide/:id" element={<IdePage />} />
      </Routes>
      <Where />
    </MemoryRouter>,
  )
  await screen.findByTestId('ide-row-a')
}

async function deleteRow(id: string) {
  fireEvent.contextMenu(screen.getByTestId(`ide-row-${id}`))
  fireEvent.click(await screen.findByTestId('ide-menu-close'))
  await waitFor(() => expect(chat.closeSession).toHaveBeenCalledWith(id))
}

const where = () => screen.getByTestId('where').textContent

describe('/ide closing the open session', () => {
  let store: Map<string, string>
  beforeEach(() => {
    vi.clearAllMocks()
    store = new Map()
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    })
    vi.stubGlobal('confirm', () => true)
    chat.listSessions.mockResolvedValue([session('a', 1), session('b', 2), session('c', 3)])
    chat.closeSession.mockResolvedValue({ ok: true, closing: true, reason: '' })
    harness.listRunners.mockResolvedValue([])
    agents.listMyAgents.mockResolvedValue({ items: [] })
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('opens the session below when a delete is relayed to a runner, without waiting for it', async () => {
    await open('/ide/b')
    await deleteRow('b')
    await waitFor(() => expect(where()).toBe('/ide/c'))
    expect(screen.getByTestId('ide-note').textContent).toContain('Deleting “b” on box…')
  })

  it('opens the session below when the delete finishes on the server', async () => {
    chat.closeSession.mockResolvedValue({ ok: true, closing: false, reason: '' })
    await open('/ide/a')
    await deleteRow('a')
    await waitFor(() => expect(where()).toBe('/ide/b'))
  })

  it('shows the empty pane when nothing is below', async () => {
    await open('/ide/c')
    await deleteRow('c')
    await waitFor(() => expect(where()).toBe('/ide'))
    expect(screen.getByText('Pick a session on the left.')).toBeTruthy()
  })

  it('stays put when the row deleted is not the one open', async () => {
    await open('/ide/a')
    await deleteRow('c')
    await screen.findByTestId('ide-note')
    expect(where()).toBe('/ide/a')
  })

  it('stays on the session, and says why, when the delete is refused', async () => {
    chat.closeSession.mockResolvedValue({ ok: false, closing: false, reason: 'unavailable' })
    await open('/ide/b')
    await deleteRow('b')
    await screen.findByTestId('ide-note')
    expect(where()).toBe('/ide/b')
  })

  it('does the same for a close from the chat pane: relayed, the row is marked and the next one opens', async () => {
    await open('/ide/a')
    fireEvent.click(screen.getByText('pane-relayed'))
    await waitFor(() => expect(where()).toBe('/ide/b'))
    expect(screen.getByTestId('ide-note').textContent).toContain('Deleting “a” on box…')
  })

  it('does the same for a close from the chat pane: finished, the list is re-read', async () => {
    await open('/ide/a')
    const reads = chat.listSessions.mock.calls.length
    fireEvent.click(screen.getByText('pane-closed'))
    await waitFor(() => expect(where()).toBe('/ide/b'))
    await waitFor(() => expect(chat.listSessions.mock.calls.length).toBeGreaterThan(reads))
  })
})

describe('/ide a session started from another', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
    vi.stubGlobal('confirm', () => true)
    chat.listSessions.mockResolvedValue([
      session('a', 1),
      { ...session('k', 2, 'other'), parent_session_id: 'a' },
    ])
    chat.closeSession.mockResolvedValue({ ok: true, closing: false, reason: '' })
    harness.listRunners.mockResolvedValue([])
    agents.listMyAgents.mockResolvedValue({ items: [] })
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('opens its own menu when right-clicked under its parent, not the browser one', async () => {
    await open('/ide/a')
    const link = await screen.findByTestId('ide-link-a-k')
    const notCancelled = fireEvent.contextMenu(link)
    expect(notCancelled).toBe(false)
    fireEvent.click(await screen.findByTestId('ide-menu-close'))
    await waitFor(() => expect(chat.closeSession).toHaveBeenCalledWith('k'))
  })
})
