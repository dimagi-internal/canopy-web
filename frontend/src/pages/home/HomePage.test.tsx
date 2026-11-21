// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { Attention, AttentionRow } from '@/api/attention'
import type { ChatSession } from '@/api/chat'

const loadAttention = vi.fn<() => Promise<Attention>>()
const hide = vi.fn(async () => 1)
const unhide = vi.fn(async () => {})
vi.mock('@/api/attention', async (orig) => ({
  ...(await orig<typeof import('@/api/attention')>()),
  loadAttention: () => loadAttention(),
  hide: (...a: unknown[]) => hide(...(a as [])),
  unhide: (...a: unknown[]) => unhide(...(a as [])),
}))
const listSessions = vi.fn<() => Promise<ChatSession[]>>(async () => [])
vi.mock('@/api/chat', async (orig) => ({
  ...(await orig<typeof import('@/api/chat')>()),
  listSessions: () => listSessions(),
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

/** A session row as `/api/me/sessions/` returns it — only the fields Home reads. */
const session = (over: Partial<ChatSession>): ChatSession => ({
  id: 's1', title: 'Fix the deploy', status: 'active', workspace: 'connect', agent_slug: 'eva', emdash_project: '',
  project: '', started_by_you: true, running: false, waiting_on_you: false, sensitive: false,
  last_activity_at: new Date().toISOString(), ...over,
}) as unknown as ChatSession

const empty: Attention = { now: [], quiet: [], hidden: [] }

describe('HomePage', () => {
  it('offers an invite the person has not accepted yet', async () => {
    // Signing in joins nothing; without this, someone who signed in instead of
    // opening their invite link had no way back to it.
    loadAttention.mockResolvedValue({ now: [], quiet: [], hidden: [] })
    listMyInvites.mockResolvedValue([{
      token: 'tok123', workspace_slug: 'marshellis', workspace_display_name: 'Marshellis',
      role: 'editor', invited_by_email: 'jj@dimagi.com', expires_at: new Date().toISOString(),
    }])
    show()
    expect(await screen.findByText('Marshellis')).toBeTruthy()
    expect(screen.getByText('Accept →').getAttribute('href')).toBe('/invite/tok123')
  })

  it('lists every open session you started, idle ones too, and none of anyone else', async () => {
    loadAttention.mockResolvedValue(empty)
    listSessions.mockResolvedValue([
      session({ id: 'a', title: 'My idle chat' }),
      session({ id: 'b', title: 'Their chat', started_by_you: false }),
      session({ id: 'c', title: 'My archived chat', status: 'archived' }),
    ])
    show()
    expect(await screen.findByText('My idle chat')).toBeTruthy()
    expect(screen.queryByText('Their chat')).toBeNull()
    expect(screen.queryByText('My archived chat')).toBeNull()
    expect(screen.getByText('My idle chat').closest('a')?.getAttribute('href')).toBe('/w/connect/sessions/a')
  })

  it('says when a session needs an answer', async () => {
    loadAttention.mockResolvedValue(empty)
    listSessions.mockResolvedValue([session({ waiting_on_you: true })])
    show()
    expect(await screen.findByText('needs an answer')).toBeTruthy()
  })

  it('leads with sessions and shows tasks open below them, each titled with a link to its page', async () => {
    loadAttention.mockResolvedValue({ ...empty, now: [item({})] })
    listSessions.mockResolvedValue([session({})])
    show()
    const s = await screen.findByText('Fix the deploy')
    const task = screen.getByText('Send EOI?')
    expect(s.compareDocumentPosition(task) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(task.closest('a')?.getAttribute('href')).toBe('/w/connect/tasks/CON-1')
    // Still foldable by hand.
    fireEvent.click(screen.getByText('Tasks and requests'))
    expect(screen.queryByText('Send EOI?')).toBeNull()
  })

  it('groups sessions by agent or project, newest group first', async () => {
    const t = (h: number) => new Date(Date.now() - h * 36e5).toISOString()
    loadAttention.mockResolvedValue(empty)
    listSessions.mockResolvedValue([
      session({ id: 'c', title: 'Older repo chat', agent_slug: 'hal', emdash_project: 'canopy-web', last_activity_at: t(3) }),
      session({ id: 'a', title: 'Repo chat', agent_slug: 'hal', emdash_project: 'canopy-web', last_activity_at: t(1) }),
      session({ id: 'b', title: 'Eva chat', agent_slug: 'eva', emdash_project: '', last_activity_at: t(2) }),
    ])
    show()
    await screen.findByText('Repo chat')
    const text = document.body.textContent ?? ''
    const order = ['canopy-web', 'Repo chat', 'Older repo chat', 'eva', 'Eva chat'].map((w) => text.indexOf(w))
    expect(order).toEqual([...order].sort((x, y) => x - y))
  })

  it('shows a running session as live, and folds a group', async () => {
    loadAttention.mockResolvedValue(empty)
    listSessions.mockResolvedValue([
      session({ agent_slug: 'hal', emdash_project: 'canopy-web' }),
      session({ id: 'r', title: 'Busy one', running: true, agent_slug: 'hal', emdash_project: 'canopy-web' }),
    ])
    show()
    expect(await screen.findByText('Busy one')).toBeTruthy()
    expect(screen.getByText('live')).toBeTruthy()
    fireEvent.click(screen.getByText('canopy-web'))
    expect(screen.queryByText('Busy one')).toBeNull()
    expect(screen.queryByText('Fix the deploy')).toBeNull()
  })

  it('starts a task in place by its key, pinned to its workspace, and says its agent starts now', async () => {
    loadAttention.mockResolvedValue({ now: [item({})], quiet: [], hidden: [] })
    show()
    const start = await screen.findByRole('button', { name: 'Start now' })
    expect(start.getAttribute('title')).toContain('Eva starts on it now')
    expect(screen.getByText(/Start now: Eva starts on it now/)).toBeTruthy()
    fireEvent.click(start)
    await waitFor(() => expect(actOn).toHaveBeenCalledWith('CON-1', 'approve', '', 'connect'))
  })

  it('declines and replies through the same task route', async () => {
    loadAttention.mockResolvedValue({ now: [item({})], quiet: [], hidden: [] })
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Reply' }))
    fireEvent.change(screen.getByPlaceholderText('Reply to eva…'), { target: { value: 'Yes, send it' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(actOn).toHaveBeenCalledWith('CON-1', 'reply', 'Yes, send it', 'connect'))
  })

  it('hides without deciding anything, and can undo', async () => {
    loadAttention.mockResolvedValue({ ...empty, now: [item({})] })
    show()
    fireEvent.click(await screen.findByText('Hide'))
    await waitFor(() => expect(hide).toHaveBeenCalledWith(['task:eva:T1'], undefined))
    expect(actOn).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Undo'))
    await waitFor(() => expect(unhide).toHaveBeenCalledWith('task:eva:T1'))
  })

  it('folds what has gone quiet and hides all of it at once', async () => {
    const old = new Date(Date.now() - 10 * 864e5).toISOString()
    loadAttention.mockResolvedValue({
      now: [],
      quiet: [item({ key: 'task:eva:T2', title: 'Old one', last_activity_at: old }),
        item({ key: 'task:eva:T3', title: 'Older', last_activity_at: old })],
      hidden: [],
    })
    listSessions.mockResolvedValue([])
    show()
    expect(await screen.findByText('You have no open sessions.')).toBeTruthy()
    expect(screen.queryByText('Old one')).toBeNull()
    fireEvent.click(screen.getByText('Hide all 2'))
    await waitFor(() => expect(hide).toHaveBeenCalledWith(['task:eva:T2', 'task:eva:T3'], undefined))
  })

  it('puts open alerts in their own section above the sessions, with no hide', async () => {
    loadAttention.mockResolvedValue(empty)
    listSessions.mockResolvedValue([session({})])
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
    const row = await screen.findByText('Fix the deploy')
    // The alert comes first in the page, and its only way off is the shared dismiss.
    expect(alertRow.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(alertRow.textContent).not.toContain('Hide')
  })

  it('shows no alerts section when nothing is open or dismissed', async () => {
    loadAttention.mockResolvedValue(empty)
    listSessions.mockResolvedValue([session({})])
    loadAlerts.mockImplementation(async () => [])
    show()
    await screen.findByText('Fix the deploy')
    expect(screen.queryByTestId('alert-list')).toBeNull()
  })
})
