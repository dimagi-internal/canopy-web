// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { Attention, AttentionRow } from '@/api/attention'

const loadAttention = vi.fn<() => Promise<Attention>>()
const letGo = vi.fn(async () => 1)
const takeBack = vi.fn(async () => {})
vi.mock('@/api/attention', async (orig) => ({
  ...(await orig<typeof import('@/api/attention')>()),
  loadAttention: () => loadAttention(),
  letGo: (...a: unknown[]) => letGo(...(a as [])),
  takeBack: (...a: unknown[]) => takeBack(...(a as [])),
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
  vi.clearAllMocks()
})

const show = () => render(<MemoryRouter><HomePage /></MemoryRouter>)

describe('HomePage', () => {
  it('approves a task in place, pinned to its workspace', async () => {
    loadAttention.mockResolvedValue({ now: [item({})], quiet: [], let_go: [] })
    show()
    fireEvent.click(await screen.findByText('Approve'))
    await waitFor(() => expect(actOnTask).toHaveBeenCalledWith('eva', 'T1', 'approve', '', 'connect'))
  })

  it('lets go without deciding, and can undo', async () => {
    loadAttention.mockResolvedValue({ now: [item({})], quiet: [], let_go: [] })
    show()
    fireEvent.click(await screen.findByText('Let go'))
    await waitFor(() => expect(letGo).toHaveBeenCalledWith(['task:eva:T1'], undefined))
    expect(actOnTask).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Undo'))
    await waitFor(() => expect(takeBack).toHaveBeenCalledWith('task:eva:T1'))
  })

  it('folds what has gone quiet and lets all of it go at once', async () => {
    const old = new Date(Date.now() - 10 * 864e5).toISOString()
    loadAttention.mockResolvedValue({
      now: [],
      quiet: [item({ key: 'task:eva:T2', title: 'Old one', last_activity_at: old }),
        item({ key: 'task:eva:T3', title: 'Older', last_activity_at: old })],
      let_go: [],
    })
    show()
    expect(await screen.findByText(/Nothing has happened that needs you/)).toBeTruthy()
    expect(screen.queryByText('Old one')).toBeNull()
    fireEvent.click(screen.getByText('Let go of all 2'))
    await waitFor(() => expect(letGo).toHaveBeenCalledWith(['task:eva:T2', 'task:eva:T3'], undefined))
  })
})
