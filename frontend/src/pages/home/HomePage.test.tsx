// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { Attention, AttentionRow } from '@/api/attention'

const loadAttention = vi.fn<() => Promise<Attention>>()
const hide = vi.fn(async () => 1)
const unhide = vi.fn(async () => {})
vi.mock('@/api/attention', async (orig) => ({
  ...(await orig<typeof import('@/api/attention')>()),
  loadAttention: () => loadAttention(),
  hide: (...a: unknown[]) => hide(...(a as [])),
  unhide: (...a: unknown[]) => unhide(...(a as [])),
}))
const actOnTask = vi.fn(async () => ({}))
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  actOnTask: (...a: unknown[]) => actOnTask(...(a as [])),
}))
vi.mock('@/api/client.v2', async (orig) => ({
  ...(await orig<typeof import('@/api/client.v2')>()),
  apiV2: { GET: async () => ({ data: [] }) },
}))
vi.mock('@/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ workspaces: [{ slug: 'connect', display_name: 'Connect', parent: null }], loading: false }),
}))
vi.mock('@/auth/AuthProvider', () => ({
  useAuth: () => ({ status: 'authenticated', user: { name: 'Jonathan', email: 'jj@dimagi.com' } }),
}))

const { HomePage } = await import('./HomePage')

const item = (over: Partial<AttentionRow>): AttentionRow => ({
  key: 'task:eva:T1', kind: 'task', reason: 'Approve or decline', title: 'Send EOI?', body: '',
  workspace: 'connect', agent_slug: 'eva', project: '', url: '/w/connect/agents/eva/tasks?waiting=me',
  last_activity_at: new Date().toISOString(), actions: ['approve', 'decline', 'reply'],
  target: { agent: 'eva', task: 'T1' }, ...over,
})

afterEach(() => {
  cleanup()
  try { window.localStorage.clear() } catch { /* none */ }
  vi.clearAllMocks()
})

const show = () => render(<MemoryRouter><HomePage /></MemoryRouter>)

const chat = (over: Partial<AttentionRow>): AttentionRow =>
  item({ key: 'session:s1', kind: 'session', reason: 'Your move', title: 'Fix the deploy', url: '/chat/s1',
    actions: ['open'], target: { session: 's1' }, ...over })

describe('HomePage', () => {
  it('leads with sessions and folds tasks under a count', async () => {
    loadAttention.mockResolvedValue({ now: [item({}), chat({})], quiet: [], hidden: [], running: [] })
    show()
    expect(await screen.findByText('Fix the deploy')).toBeTruthy()
    // The task is behind its fold until opened.
    expect(screen.queryByText('Send EOI?')).toBeNull()
    fireEvent.click(screen.getByText('Tasks and requests'))
    expect(await screen.findByText('Send EOI?')).toBeTruthy()
  })

  it('groups sessions by agent or project, newest group first', async () => {
    const t = (h: number) => new Date(Date.now() - h * 36e5).toISOString()
    loadAttention.mockResolvedValue({
      now: [
        chat({ key: 'session:a', title: 'Repo chat', agent_slug: 'hal', project: 'canopy-web', last_activity_at: t(1) }),
        chat({ key: 'session:b', title: 'Eva chat', agent_slug: 'eva', project: '', last_activity_at: t(2) }),
        chat({ key: 'session:c', title: 'Older repo chat', agent_slug: 'hal', project: 'canopy-web', last_activity_at: t(3) }),
      ],
      quiet: [],
      hidden: [],
      running: [],
    })
    show()
    await screen.findByText('Repo chat')
    const text = document.body.textContent ?? ''
    const order = ['canopy-web', 'Repo chat', 'Older repo chat', 'eva', 'Eva chat'].map((w) => text.indexOf(w))
    expect(order).toEqual([...order].sort((x, y) => x - y))
  })

  it('lists running sessions in their group, and folds a group', async () => {
    loadAttention.mockResolvedValue({
      now: [chat({ agent_slug: 'hal', project: 'canopy-web' })],
      quiet: [],
      hidden: [],
      running: [chat({ key: 'session:r', title: 'Busy one', reason: 'Running', agent_slug: 'hal', project: 'canopy-web' })],
    })
    show()
    expect(await screen.findByText('Busy one')).toBeTruthy()
    expect(screen.getByText('live')).toBeTruthy()
    fireEvent.click(screen.getByText('canopy-web'))
    expect(screen.queryByText('Busy one')).toBeNull()
    expect(screen.queryByText('Fix the deploy')).toBeNull()
  })

  it('approves a task in place, pinned to its workspace', async () => {
    loadAttention.mockResolvedValue({ now: [item({})], quiet: [], hidden: [], running: [] })
    show()
    fireEvent.click(await screen.findByText('Tasks and requests'))
    fireEvent.click(await screen.findByText('Approve'))
    await waitFor(() => expect(actOnTask).toHaveBeenCalledWith('eva', 'T1', 'approve', '', 'connect'))
  })

  it('hides without deciding anything, and can undo', async () => {
    loadAttention.mockResolvedValue({ now: [chat({})], quiet: [], hidden: [], running: [] })
    show()
    fireEvent.click(await screen.findByText('Hide'))
    await waitFor(() => expect(hide).toHaveBeenCalledWith(['session:s1'], undefined))
    expect(actOnTask).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Undo'))
    await waitFor(() => expect(unhide).toHaveBeenCalledWith('session:s1'))
  })

  it('folds what has gone quiet and hides all of it at once', async () => {
    const old = new Date(Date.now() - 10 * 864e5).toISOString()
    loadAttention.mockResolvedValue({
      now: [],
      quiet: [item({ key: 'task:eva:T2', title: 'Old one', last_activity_at: old }),
        item({ key: 'task:eva:T3', title: 'Older', last_activity_at: old })],
      hidden: [],
      running: [],
    })
    show()
    expect(await screen.findByText('No session is waiting on you or running.')).toBeTruthy()
    expect(screen.queryByText('Old one')).toBeNull()
    fireEvent.click(screen.getByText('Hide all 2'))
    await waitFor(() => expect(hide).toHaveBeenCalledWith(['task:eva:T2', 'task:eva:T3'], undefined))
  })
})
