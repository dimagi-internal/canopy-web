// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { getSkillHistory } from '@/api/agents'

const H = {
  agent: 'ace', repo_url: 'x', head_sha: 'b', synced_at: '2026-04-20T00:00:00Z', synced_with: 'owner-gh',
  last_error: '', credential_state: 'ok',
  groups: [{ title: 'One', kind: 'phase', num: '01', skills: ['alpha'] }],
  checks: {}, present: ['alpha'],
  commits: [{ sha: 'a1', date: '2026-04-01', subject: 'feat: alpha' }, { sha: 'b2', date: '2026-04-05', subject: 'fix: alpha grows' }],
  skills: [{ name: 'alpha', revisions: [[0, 10, 10, 0], [1, 12, 2, 0]] }],
}

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getSkillHistory: vi.fn(async () => H),
  syncSkillHistory: vi.fn(async () => H),
}))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({ agent: { slug: 'ace', name: 'ACE' } }),
}))

const { AgentHistorySection } = await import('./AgentHistorySection')

function Where() {
  const l = useLocation()
  return <div data-testid="where">{l.search}</div>
}

function renderAt(search = '') {
  render(
    <MemoryRouter initialEntries={[`/w/connect/agents/ace/history${search}`]}>
      <Routes><Route path="/w/:workspace/agents/:slug/history" element={<><AgentHistorySection /><Where /></>} /></Routes>
    </MemoryRouter>,
  )
}

afterEach(() => cleanup())

describe('AgentHistorySection', () => {
  it('drills all skills → group → skill → commit and back through the breadcrumb', async () => {
    renderAt()
    fireEvent.click(await screen.findByRole('button', { name: /Open One/ }))
    expect(screen.getByTestId('where').textContent).toContain('group=One')

    fireEvent.click(screen.getByRole('button', { name: 'Skill alpha' }))
    expect(screen.getByTestId('where').textContent).toContain('skill=alpha')
    expect(screen.getByText('fix: alpha grows')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /fix: alpha grows/ }))
    expect(screen.getByTestId('where').textContent).toContain('commit=b2')

    fireEvent.click(screen.getByRole('button', { name: 'All skills' }))
    expect(screen.getByTestId('where').textContent).toBe('')
  })

  it('restores a selection from the URL', async () => {
    renderAt('?skill=alpha&at=2026-04-02')
    expect(await screen.findByText(/Created Apr 1/)).toBeTruthy()
  })

  it('says whose GitHub access produced the history', async () => {
    renderAt()
    expect(await screen.findByText(/owner-gh/)).toBeTruthy()
  })

  it('a selection made during playback wins, and stops playback', async () => {
    renderAt()
    // Wait for the real initial load (a genuine microtask) before switching
    // to fake timers, so the async data-fetch isn't affected by them.
    await screen.findByRole('button', { name: /Open One/ })

    vi.useFakeTimers()
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Play from the first commit' }))
      expect(screen.getByRole('button', { name: 'Pause' })).toBeTruthy()

      fireEvent.click(screen.getByRole('button', { name: 'Skill alpha' }))
      vi.advanceTimersByTime(120)

      expect(screen.getByTestId('where').textContent).toContain('skill=alpha')
      expect(screen.getByRole('button', { name: 'Play from the first commit' })).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows a retry affordance when the initial load fails, and recovers on retry', async () => {
    const mocked = vi.mocked(getSkillHistory)
    mocked.mockRejectedValueOnce(new Error('network down'))
    renderAt()
    expect(await screen.findByText(/Couldn't load this agent's history/)).toBeTruthy()

    mocked.mockResolvedValueOnce(H as never)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('button', { name: /Open One/ })).toBeTruthy()
  })
})
