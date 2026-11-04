// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { listAgentSkills, type AgentSkillOut } from '@/api/agents'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { AgentSkillsSection } from './AgentSkillsSection'

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listAgentSkills: vi.fn(),
}))

function skill(name: string, internal: boolean, id: number): AgentSkillOut {
  return {
    id, agent_slug: 'trellis', name, description: '', url: '', improvement_note: '',
    launchable: false, args_hint: '', internal, updated_at: '2026-11-04T00:00:00Z',
  }
}

function Workspace() {
  return <Outlet context={{ agent: { slug: 'trellis', name: 'Trellis' } } as unknown as AgentOutletContext} />
}

function renderSection() {
  render(
    <MemoryRouter initialEntries={['/skills']}>
      <Routes>
        <Route element={<Workspace />}>
          <Route path="skills" element={<AgentSkillsSection />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(cleanup)

describe('AgentSkillsSection', () => {
  it('counts every skill, shows internal ones marked, and says how many are usable commands', async () => {
    vi.mocked(listAgentSkills).mockResolvedValue([
      skill('turn', false, 1), skill('coach', false, 2), skill('shipping', true, 3),
    ])
    renderSection()
    expect(await screen.findByText('shipping')).toBeTruthy()
    expect(screen.getByText('3')).toBeTruthy()
    expect(screen.getByText(/^2 you can start with/)).toBeTruthy()
    expect(screen.getByText(/1 internal/)).toBeTruthy()
    expect(screen.getAllByText('Internal')).toHaveLength(1)
  })

  it('says nothing about commands when no skill is internal', async () => {
    vi.mocked(listAgentSkills).mockResolvedValue([skill('turn', false, 1)])
    renderSection()
    expect(await screen.findByText('turn')).toBeTruthy()
    expect(screen.queryByText(/you can start with/)).toBeNull()
    expect(screen.queryByText('Internal')).toBeNull()
  })
})
