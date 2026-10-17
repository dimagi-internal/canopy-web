// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentHealthOut, WorkspaceHealthOut } from '@/api/workspaces'

const step = (key: string, status: 'done' | 'todo', href = '') => ({
  key, title: `Step ${key}`, status, detail: `detail ${key}`, where: 'web' as const, href, action: `Fix ${key}`, command: '',
})

function agent(slug: string, over: Partial<AgentHealthOut> = {}): AgentHealthOut {
  return {
    slug, name: slug.toUpperCase(), workspace: 'connect', owner_email: `${slug}@x.org`, can_fix: true, email: '',
    repo_url: '', turn_mode: 'manual', has_interface: false, skills: 2, schedules: 0, created_at: '2026-10-01T00:00:00Z',
    last_turn_at: null, turns_30d: 0, left: 1,
    vault: { source: 'canopy-web', state: 'moving', declared: 2, refs: [{ name: 'gh', status: 'stored' }, { name: 'old', status: '1password' }],
      stored: ['gh'], in_1password: ['old'], missing: [], op_vault: '', op_key_set: false, zero_knowledge: true, machines_waiting: [] },
    steps: [step('repo', 'done'), step('vault', 'todo', '/w/connect/agents/x/settings#credentials')],
    ...over,
  }
}

const HEALTH: WorkspaceHealthOut = {
  root: 'dimagi',
  workspaces: [
    { slug: 'dimagi', name: 'Dimagi', depth: 0, parent: '', agents: 0, shared_vault: { vault: 'Fleet', key_set: true, inherited_from: '' } },
    { slug: 'connect', name: 'Connect', depth: 1, parent: 'dimagi', agents: 2, shared_vault: { vault: 'Fleet', key_set: true, inherited_from: 'dimagi' } },
  ],
  agents: [agent('hal'), agent('eva', { can_fix: false, left: 1 }),
    agent('ada', { left: 0, vault: { ...agent('ada').vault, state: 'canopy-web', in_1password: [] } })],
  totals: { agents: 3, set_up: 1, needs_setup: 2, vault_canopy_web: 1, vault_moving: 2, vault_1password: 0, vault_none: 0 },
}

vi.mock('@/api/workspaces', async (orig) => ({
  ...(await orig<typeof import('@/api/workspaces')>()),
  getWorkspaceHealth: vi.fn(async () => HEALTH),
}))

const { WorkspaceHealthPage } = await import('./WorkspaceHealthPage')

afterEach(() => cleanup())

function renderPage() {
  render(
    <MemoryRouter initialEntries={['/w/dimagi/health']}>
      <Routes><Route path="/w/:workspace/health" element={<WorkspaceHealthPage />} /></Routes>
    </MemoryRouter>,
  )
}

describe('WorkspaceHealthPage', () => {
  it('lists every agent in the tree and filters to the ones not on the canopy-web vault', async () => {
    renderPage()
    expect(await screen.findByText('HAL')).toBeTruthy()
    expect(screen.getByText('ADA')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Not on canopy-web vault' }))
    expect(screen.queryByText('ADA')).toBeNull()
    expect(screen.getByText('HAL')).toBeTruthy()
  })

  it('links the fix when the viewer may make it, and offers the owner an email when not', async () => {
    renderPage()
    fireEvent.click(await screen.findByText('HAL'))
    expect(screen.getByRole('link', { name: 'Fix vault' }).getAttribute('href')).toBe('/w/connect/agents/x/settings#credentials')
    expect(screen.getByText('in 1Password')).toBeTruthy()

    fireEvent.click(screen.getByText('EVA'))
    // One agent open at a time, and EVA's fix is not the viewer's to follow.
    expect(screen.queryAllByRole('link', { name: 'Fix vault' })).toHaveLength(0)
    const mail = screen.getByTestId('send-to-owner').getAttribute('href') ?? ''
    expect(mail.startsWith('mailto:eva%40x.org?subject=')).toBe(true)
    expect(decodeURIComponent(mail)).toContain('Step vault: detail vault')
  })
})
