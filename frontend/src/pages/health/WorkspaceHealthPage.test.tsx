// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentHealthOut, WorkspaceHealthOut } from '@/api/workspaces'

const step = (key: string, status: 'done' | 'todo', href = '') => ({
  key, title: `Step ${key}`, status, detail: `detail ${key}`, where: 'web' as const, href, action: `Fix ${key}`, command: '',
  links: [], only_to_run_on_its_own: false,
})

function agent(slug: string, over: Partial<AgentHealthOut> = {}): AgentHealthOut {
  return {
    slug, name: slug.toUpperCase(), workspace: 'connect', owner_email: `${slug}@x.org`, can_fix: true, email: '',
    repo_url: '', turn_mode: 'manual', has_interface: false, capabilities: 0, full_access: [], skills: 2, schedules: 0,
    projects: 1, open_tasks: 3, waiting_tasks: 1, created_at: '2026-10-01T00:00:00Z',
    active_sessions: 1, last_session_at: '2026-10-17T00:00:00Z',
    recent_sessions: [{ id: 's1', workspace: 'connect', title: `${slug} chat`, at: '2026-10-17T00:00:00Z', active: true }],
    last_turn_at: null, turns_30d: 0, done_30d: 0, failed_30d: 0, people_30d: 0, ran_on: [], left: 1,
    vault: { state: 'moving', declared: 2, refs: [{ name: 'gh', status: 'stored' }, { name: 'old', status: 'missing' }],
      stored: ['gh'], missing: ['old'], zero_knowledge: true, machines_waiting: [] },
    steps: [step('repo', 'done'), step('vault', 'todo', '/w/connect/agents/x/settings#credentials')],
    ...over,
  }
}

const HEALTH: WorkspaceHealthOut = {
  root: 'dimagi',
  workspaces: [
    { slug: 'dimagi', name: 'Dimagi', depth: 0, parent: '', agents: 0 },
    { slug: 'connect', name: 'Connect', depth: 1, parent: 'dimagi', agents: 2 },
  ],
  agents: [agent('hal', { turns_30d: 6, done_30d: 4, failed_30d: 2, people_30d: 1,
    ran_on: [{ name: 'shayoni-win-cdp', kind: 'emdash', turns: 5 }, { name: 'cloud-ec2-2', kind: 'cloud', turns: 1 }] }),
    agent('eva', { can_fix: false, left: 1 }),
    agent('ada', { left: 0, vault: { ...agent('ada').vault!, state: 'canopy-web', missing: [] } })],
  runners: [{ id: '00000000-0000-0000-0000-000000000001', name: 'laptop', kind: 'emdash', location: 'local', status: 'online',
    ready: true, ready_note: '', paused: false, host: 'h', last_heartbeat_at: '2026-10-17T00:00:00Z', workspace: 'dimagi',
    in_tree: true, owner_email: 'jj@x.org', flags: [], agent_count: 2, directed: false, code_current: false, machine: '', serves: '',
    usage_reported_at: null, can_administer: false,
    status_note: 'emdash CDP unreachable on :9223', remedy: 'open Emdash CDP', code_sha: 'aaaa1111', expected_code_sha: 'bbbb2222',
    agents: [{ slug: 'hal', name: 'Hal', workspace: 'connect' }],
    problems: [{ name: 'disk', status: 'warn', detail: '90% full' }] }],
  totals: { agents: 3, set_up: 1, needs_setup: 2, used_30d: 1, vault_canopy_web: 1, vault_moving: 2, vault_none: 0, runners: 1, runners_online: 1 },
}

// The page reads the table without the checklists (`setup: false`), then each
// agent's checklist and vault from `/health/setup`.
const TABLE: WorkspaceHealthOut = {
  ...HEALTH,
  agents: HEALTH.agents.map((a) => ({ ...a, steps: null, vault: null, left: null })),
  totals: { ...HEALTH.totals, set_up: null, needs_setup: null, vault_canopy_web: null, vault_moving: null,
    vault_none: null },
}
const getWorkspaceHealth = vi.fn<(slug: string, opts?: { setup?: boolean }) => Promise<WorkspaceHealthOut>>(async () => TABLE)
const getWorkspaceHealthSetup = vi.fn(async (_slug: string, names: readonly string[]) =>
  HEALTH.agents.filter((a) => names.includes(a.slug))
    .map((a) => ({ slug: a.slug, steps: a.steps!, vault: a.vault!, left: a.left! })))
vi.mock('@/api/workspaces', async (orig) => ({
  ...(await orig<typeof import('@/api/workspaces')>()),
  getWorkspaceHealth: (slug: string, opts?: { setup?: boolean }) => getWorkspaceHealth(slug, opts),
  getWorkspaceHealthSetup: (slug: string, names: readonly string[]) => getWorkspaceHealthSetup(slug, names),
}))

const { WorkspaceHealthPage, whoCanUse } = await import('./WorkspaceHealthPage')

afterEach(() => { cleanup(); vi.clearAllMocks() })

function renderPage() {
  render(
    <MemoryRouter initialEntries={['/w/dimagi/health']}>
      <Routes><Route path="/w/:workspace/health" element={<WorkspaceHealthPage />} /></Routes>
    </MemoryRouter>,
  )
}

/** The page, once every checklist is in. */
async function renderLoaded() {
  renderPage()
  await screen.findByText('HAL')
  await waitFor(() => expect(screen.queryAllByLabelText('loading')).toHaveLength(0))
}

describe('reading the checklists after the table', () => {
  it('shows the table before any checklist is in, then fills each row and the totals', async () => {
    let release = () => {}
    getWorkspaceHealthSetup.mockImplementationOnce(async (_s, names) => {
      await new Promise<void>((r) => { release = r })
      return HEALTH.agents.filter((a) => names.includes(a.slug))
        .map((a) => ({ slug: a.slug, steps: a.steps!, vault: a.vault!, left: a.left! }))
    })
    renderPage()
    const hal = (await screen.findByText('HAL')).closest('tr')!
    expect(getWorkspaceHealth).toHaveBeenCalledWith('dimagi', { setup: false })
    expect(within(hal).getAllByLabelText('loading')).toHaveLength(2)
    expect(screen.getByRole('button', { name: /Fully set up/ }).textContent).toContain('…')
    // `release` is assigned only once the page has made the setup call; releasing
    // before that resolves nothing and the row never fills (flaked the merge queue).
    await waitFor(() => expect(getWorkspaceHealthSetup).toHaveBeenCalled())
    release()
    await waitFor(() => expect(within(hal).getByLabelText('1 steps left')).toBeTruthy())
    expect(screen.getByRole('button', { name: /Fully set up/ }).textContent).toContain('1')
    expect(getWorkspaceHealthSetup).toHaveBeenCalledWith('dimagi', ['hal', 'eva', 'ada'])
  })

  it('says which rows could not be read, and still counts the rest', async () => {
    getWorkspaceHealthSetup.mockRejectedValueOnce(new Error('boom'))
    renderPage()
    const hal = (await screen.findByText('HAL')).closest('tr')!
    await waitFor(() => expect(within(hal).getAllByText('not loaded')).toHaveLength(2))
    fireEvent.click(screen.getByText('HAL'))
    expect(screen.getByText(/could not be read/)).toBeTruthy()
  })
})

describe('WorkspaceHealthPage', () => {
  it('lists every agent in the tree and filters to the ones not on the canopy-web vault', async () => {
    await renderLoaded()
    expect(await screen.findByText('HAL')).toBeTruthy()
    expect(screen.getByText('ADA')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Not on canopy-web vault' }))
    expect(screen.queryByText('ADA')).toBeNull()
    expect(screen.getByText('HAL')).toBeTruthy()
  })

  it('links an agent’s projects and open tasks, and says who can use it in words', async () => {
    // Loaded, not just the table: an opened row shows its sessions only once
    // its checklist is in, and on a slow CI box the click could beat it.
    await renderLoaded()
    expect((await screen.findAllByRole('link', { name: '3 open tasks' }))[0].getAttribute('href')).toBe('/w/connect/agents/hal/tasks')
    expect(screen.getAllByRole('link', { name: '1 projects' })[0].getAttribute('href')).toBe('/w/connect/agents/hal/projects')
    expect(screen.getAllByText('Admins only').length).toBe(3)
    expect(screen.getAllByText('Manual · asks first').length).toBe(3)
    expect(screen.getAllByText('1 open').length).toBe(3)
    fireEvent.click(screen.getByText('HAL'))
    expect(screen.getByRole('link', { name: 'hal chat' }).getAttribute('href')).toBe('/w/connect/sessions/s1')
  })

  it('links the fix when the viewer may make it, and offers the owner an email when not', async () => {
    await renderLoaded()
    fireEvent.click(await screen.findByText('HAL'))
    expect(screen.getByRole('link', { name: 'Fix vault' }).getAttribute('href')).toBe('/w/connect/agents/x/settings#credentials')
    expect(screen.getByText('not stored anywhere')).toBeTruthy()

    fireEvent.click(screen.getByText('EVA'))
    // One agent open at a time, and EVA's fix is not the viewer's to follow.
    expect(screen.queryAllByRole('link', { name: 'Fix vault' })).toHaveLength(0)
    const mail = screen.getByTestId('send-to-owner').getAttribute('href') ?? ''
    expect(mail.startsWith('mailto:eva%40x.org?subject=')).toBe(true)
    expect(decodeURIComponent(mail)).toContain('Step vault: detail vault')
  })
})

describe('every figure opens its source', () => {
  it('says who used an agent with steps left, how its turns ended and where they ran', async () => {
    renderPage()
    const hal = (await screen.findAllByText('HAL'))[0].closest('tr') as HTMLElement
    expect(within(hal).getByText(/2 failed/)).toBeTruthy()
    expect(within(hal).getByTestId('ran-on').textContent).toBe('shayoni-win-cdp 5 · cloud-ec2-2 1')
    fireEvent.click(screen.getByText('Used, 30 days'))
    expect(screen.getByTestId('tile-filter').textContent).toContain('Used, 30 days')
    const table = screen.getByTestId('health-agents')
    expect(within(table).queryByText('EVA')).toBeNull()
    expect(within(table).getByText('HAL')).toBeTruthy()
  })

  it('a tile filters the agents table to what it counts', async () => {
    await renderLoaded()
    await screen.findByText('HAL')
    fireEvent.click(screen.getByRole('button', { name: /Fully set up/ }))
    expect(screen.getByTestId('tile-filter').textContent).toContain('Fully set up')
    expect(screen.queryByText('HAL')).toBeNull()
    expect(screen.getByText('ADA')).toBeTruthy()
  })

  it('links a row’s settings, turns and workspace to where each is set', async () => {
    renderPage()
    await screen.findByText('HAL')
    // The table draws before each agent's checklist arrives; its links come with it.
    const hrefs = () => [...screen.getByText('HAL').closest('tr')!.querySelectorAll('a')].map((a) => a.getAttribute('href'))
    await waitFor(() => expect(hrefs()).toEqual(expect.arrayContaining([
      '/w/connect/agents/hal/settings#credentials', '/w/connect/health', '/w/connect/agents/hal/settings#reach',
      '/w/connect/agents/hal/settings#running', 'mailto:hal@x.org',
    ])))
  })
})

describe('runners on Health', () => {
  it('lists each runner with whether its code is current and what its own checks say', async () => {
    renderPage()
    const row = (await screen.findByText('laptop')).closest('tr')!
    expect(row.textContent).toContain('update pending')
    // Counted in the row, named on hover and in the detail.
    const checks = within(row).getByTestId('runner-checks')
    expect(checks.textContent).toBe('1 warning')
    expect(checks.getAttribute('title')).toContain('disk')
  })

  it('opens a runner for why it is in its state, the fix, its checks and its agents', async () => {
    renderPage()
    fireEvent.click(await screen.findByText('laptop'))
    const detail = screen.getByTestId('runner-detail')
    expect(screen.getByTestId('runner-note').textContent).toContain('emdash CDP unreachable on :9223')
    expect(screen.getByTestId('runner-remedy').textContent).toContain('open Emdash CDP')
    expect(detail.textContent).toContain('90% full')
    expect(detail.textContent).toContain('aaaa1111')
    expect(screen.getByRole('link', { name: 'Hal' }).getAttribute('href')).toBe('/w/connect/agents/hal/settings#running')
  })
})

describe('whoCanUse', () => {
  it('names both kinds of published access, never "0 capabilities"', () => {
    expect(whoCanUse({ has_interface: false, capabilities: 0, full_access: [] })).toBe('Admins only')
    expect(whoCanUse({ has_interface: true, capabilities: 0, full_access: ['@dimagi.com:verified'] })).toBe('Admins + full: 1 rule')
    expect(whoCanUse({ has_interface: true, capabilities: 3, full_access: ['a', 'b'] })).toBe('Admins + full: 2 rules · 3 capabilities')
  })
})
