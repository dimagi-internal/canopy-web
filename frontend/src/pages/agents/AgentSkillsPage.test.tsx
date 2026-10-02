// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { AgentHistorySection } from './AgentHistorySection'
import { AgentSkillsPage } from './AgentSkillsPage'

// Deliberately NOT mocking useOutletContext (AgentHistorySection.test does):
// the bug was that this page's <Outlet /> dropped the agent workspace's
// context, so its children crashed destructuring `agent` from undefined. A
// mocked context can never see that — only the real nesting can.
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getSkillHistory: vi.fn(async () => ({
    agent: 'ace', repo_url: 'https://github.com/o/ace', head_sha: 'b'.repeat(40),
    synced_at: '2026-04-20T00:00:00Z', synced_with: 'owner-gh', last_error: '', credential_state: 'ok',
    owner_name: 'Olive Owner', viewer_is_owner: false, viewer_can_sync: true,
    install_url: 'https://github.com/apps/canopy-agents/installations/new',
    groups: [{ title: 'One', kind: 'phase', num: '01', skills: ['alpha'] }],
    checks: {}, present: ['alpha'],
    commits: [{ sha: 'b'.repeat(40), date: '2026-04-05', subject: 'feat: alpha' }],
    skills: [{ name: 'alpha', revisions: [[0, 10, 10, 0]] }],
  })),
}))

function AgentWorkspace() {
  return <Outlet context={{ agent: { slug: 'ace', name: 'ACE' } } as unknown as AgentOutletContext} />
}

afterEach(cleanup)

describe('AgentSkillsPage', () => {
  it('hands the agent workspace context through to the history tab', async () => {
    render(
      <MemoryRouter initialEntries={['/w/connect/agents/ace/skills/history']}>
        <Routes>
          <Route path="/w/:workspace/agents/:slug" element={<AgentWorkspace />}>
            <Route path="skills" element={<AgentSkillsPage />}>
              <Route path="history" element={<AgentHistorySection />} />
            </Route>
          </Route>
        </Routes>
      </MemoryRouter>,
    )
    expect(await screen.findByRole('button', { name: /Open One/ })).toBeTruthy()
  })
})
