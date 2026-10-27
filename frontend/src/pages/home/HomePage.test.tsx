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
const loadAlerts = vi.fn<(scope?: string, status?: string) => Promise<unknown[]>>(async () => [])
vi.mock('@/api/alerts', async (orig) => ({
  ...(await orig<typeof import('@/api/alerts')>()),
  loadAlerts: (scope?: string, status?: string) => loadAlerts(scope, status),
}))
const actOn = vi.fn(async () => ({}))
vi.mock('@/api/work', async (orig) => ({
  ...(await orig<typeof import('@/api/work')>()),
  actOn: (...a: unknown[]) => actOn(...(a as [])),
}))
vi.mock('@/api/client.v2', async (orig) => ({
  ...(await orig<typeof import('@/api/client.v2')>()),
  apiV2: { GET: async () => ({ data: [] }) },
}))
const listMyInvites = vi.fn<() => Promise<unknown[]>>(async () => [])
vi.mock('@/api/workspaces', async (orig) => ({
  ...(await orig<typeof import('@/api/workspaces')>()),
  listMyInvites: () => listMyInvites(),
}))
vi.mock('@/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ workspaces: [{ slug: 'connect', display_name: 'Connect', parent: null }], loading: false }),
}))
vi.mock('@/auth/AuthProvider', () => ({
  useAuth: () => ({ status: 'authenticated', user: { name: 'Jonathan', email: 'jj@dimagi.com' } }),
}))

const { HomePage } = await import('./HomePage')

const item = (over: Partial<AttentionRow>): AttentionRow => ({
  key: 'task:eva:T1', kind: 'task', state: 'asking', reason: 'Approve or decline', title: 'Send EOI?', body: '',
  workspace: 'connect', organisation: 'connect', agent_slug: 'eva', project: '', url: '/w/connect/tasks/CON-1',
  last_activity_at: new Date().toISOString(), actions: ['approve', 'decline', 'reply'],
  target: { agent: 'eva', task: 'T1', key: 'CON-1' }, private: false, ...over,
})

afterEach(() => {
  cleanup()
  try { window.localStorage.clear() } catch { /* none */ }
  vi.clearAllMocks()
})

const show = () => render(<MemoryRouter><HomePage /></MemoryRouter>)

const chat = (over: Partial<AttentionRow>): AttentionRow =>
  item({ key: 'session:s1', kind: 'session', state: 'your_move', reason: 'Your move', title: 'Fix the deploy', url: '/chat/s1',
    actions: ['open'], target: { session: 's1' }, ...over })

describe('HomePage', () => {
  it('offers an invite the person has not accepted yet', async () => {
    // Signing in joins nothing; without this, someone who signed in instead of
    // opening their invite link had no way back to it.
    loadAttention.mockResolvedValue({ now: [], quiet: [], hidden: [], running: [] })
    listMyInvites.mockResolvedValue([{
      token: 'tok123', workspace_slug: 'marshellis', workspace_display_name: 'Marshellis',
      role: 'editor', invited_by_email: 'jj@dimagi.com', expires_at: new Date().toISOString(),
    }])
    show()
    expect(await screen.findByText('Marshellis')).toBeTruthy()
    expect(screen.getByText('Accept →').getAttribute('href')).toBe('/invite/tok123')
  })

  it('shows a session held behind an away runner as parked, with the reason', async () => {
    loadAttention.mockResolvedValue({
      now: [chat({ state: 'parked', reason: 'Waiting — jj-mbp is paused' })], quiet: [], hidden: [], running: [],
    })
    show()
    expect(await screen.findByText('Fix the deploy')).toBeTruthy()
    expect(screen.getByText('Waiting — jj-mbp is paused')).toBeTruthy()
    expect(screen.queryByText('needs an answer')).toBeNull()
  })

  it('leads with sessions and shows tasks open below them, each titled with a link to its page', async () => {
    loadAttention.mockResolvedValue({ now: [item({}), chat({})], quiet: [], hidden: [], running: [] })
    show()
    const session = await screen.findByText('Fix the deploy')
    const task = screen.getByText('Send EOI?')
    expect(session.compareDocumentPosition(task) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(task.closest('a')?.getAttribute('href')).toBe('/w/connect/tasks/CON-1')
    // Still foldable by hand.
    fireEvent.click(screen.getByText('Tasks and requests'))
    expect(screen.queryByText('Send EOI?')).toBeNull()
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
      running: [chat({ key: 'session:r', title: 'Busy one', state: 'running', reason: 'Running', agent_slug: 'hal', project: 'canopy-web' })],
    })
    show()
    expect(await screen.findByText('Busy one')).toBeTruthy()
    expect(screen.getByText('live')).toBeTruthy()
    fireEvent.click(screen.getByText('canopy-web'))
    expect(screen.queryByText('Busy one')).toBeNull()
    expect(screen.queryByText('Fix the deploy')).toBeNull()
  })

  it('starts a task in place by its key, pinned to its workspace, and says its agent starts now', async () => {
    loadAttention.mockResolvedValue({ now: [item({})], quiet: [], hidden: [], running: [] })
    show()
    const start = await screen.findByRole('button', { name: 'Start now' })
    expect(start.getAttribute('title')).toContain('Eva starts on it now')
    expect(screen.getByText(/Start now: Eva starts on it now/)).toBeTruthy()
    fireEvent.click(start)
    await waitFor(() => expect(actOn).toHaveBeenCalledWith('CON-1', 'approve', '', 'connect'))
  })

  it('declines and replies through the same task route', async () => {
    loadAttention.mockResolvedValue({ now: [item({})], quiet: [], hidden: [], running: [] })
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Reply' }))
    fireEvent.change(screen.getByPlaceholderText('Reply to eva…'), { target: { value: 'Yes, send it' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(actOn).toHaveBeenCalledWith('CON-1', 'reply', 'Yes, send it', 'connect'))
  })

  it('hides without deciding anything, and can undo', async () => {
    loadAttention.mockResolvedValue({ now: [chat({})], quiet: [], hidden: [], running: [] })
    show()
    fireEvent.click(await screen.findByText('Hide'))
    await waitFor(() => expect(hide).toHaveBeenCalledWith(['session:s1'], undefined))
    expect(actOn).not.toHaveBeenCalled()
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

  it('puts open alerts in their own section above the sessions, with no hide', async () => {
    loadAttention.mockResolvedValue({ now: [chat({})], quiet: [], hidden: [], running: [] })
    loadAlerts.mockImplementation(async (_scope, status) => status === 'open' ? [{
      id: 1, kind: 'runner_credential', severity: 'blocking', title: 'The "hal" token on cloud-ec2-2 is signed out',
      body: '', url: '', workspace: 'connect', organisation: 'connect', scopes: [], status: 'open',
      first_seen_at: new Date().toISOString(), last_seen_at: new Date().toISOString(), occurrences: 1,
      closed_by_email: '', closed_at: null, last_notify: '', handler_agent: '', handler_email: '',
      actions: [{ id: 'dismiss', label: 'Dismiss', closes: true, href: '' }],
    }] : [])
    show()
    const alertRow = await screen.findByTestId('alert-row')
    expect(loadAlerts).toHaveBeenCalledWith(undefined, 'open')
    expect(screen.getByText(/^Alerts/)).toBeTruthy()
    const session = await screen.findByText('Fix the deploy')
    // The alert comes first in the page, and its only way off is the shared dismiss.
    expect(alertRow.compareDocumentPosition(session) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(alertRow.textContent).not.toContain('Hide')
  })

  it('shows no alerts section when nothing is open or dismissed', async () => {
    loadAttention.mockResolvedValue({ now: [chat({})], quiet: [], hidden: [], running: [] })
    loadAlerts.mockImplementation(async () => [])
    show()
    await screen.findByText('Fix the deploy')
    expect(screen.queryByTestId('alert-list')).toBeNull()
  })
})
