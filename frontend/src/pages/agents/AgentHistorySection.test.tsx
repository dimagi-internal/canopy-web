// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { getSkillHistory } from '@/api/agents'
import { currentPageState } from '@/widget/pageState'
import { currentSpecs, runPageAction } from '@/widget/pageActions'

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

describe('the page contract', () => {
  it('declares the selected skill and date to the assistant', async () => {
    renderAt('?skill=alpha&at=2026-04-02')
    await screen.findByText(/Created Apr 1/)
    expect(currentPageState()).toMatchObject({
      backing_tool: 'skill_history',
      resource: 'skill-history://ace',
      visible_ids: ['alpha'],
      filters: { agent: 'ace', skill: 'alpha', as_of: '2026-04-02' },
    })
  })

  it('declares nothing selected at the top level', async () => {
    renderAt()
    await screen.findByText(/owner-gh/)
    expect(currentPageState()).toMatchObject({ visible_ids: [], filters: { agent: 'ace', as_of: '2026-04-20' } })
  })

  it('offers selectSkill, showCommit and setTimeline', async () => {
    renderAt()
    await screen.findByText(/owner-gh/)
    expect(currentSpecs().map((s) => s.name)).toEqual(expect.arrayContaining(['selectSkill', 'showCommit', 'setTimeline']))
  })

  it('selectSkill moves the page, and refuses a skill that does not exist', async () => {
    renderAt()
    await screen.findByText(/owner-gh/)
    await act(() => runPageAction('selectSkill', { skill: 'alpha' }))
    expect(screen.getByTestId('where').textContent).toContain('skill=alpha')
    await expect(runPageAction('selectSkill', { skill: 'nope' })).rejects.toThrow(/no skill named nope/)
  })

  it('showCommit accepts a short sha and refuses an unknown one', async () => {
    renderAt()
    await screen.findByText(/owner-gh/)
    await act(() => runPageAction('showCommit', { sha: 'b2' }))
    expect(screen.getByTestId('where').textContent).toContain('commit=b2')
    await expect(runPageAction('showCommit', { sha: 'zzz' })).rejects.toThrow(/no commit/)
  })

  it('setTimeline refuses a date outside the history', async () => {
    renderAt()
    await screen.findByText(/owner-gh/)
    await act(() => runPageAction('setTimeline', { date: '2026-04-03' }))
    expect(screen.getByTestId('where').textContent).toContain('at=2026-04-03')
    await expect(runPageAction('setTimeline', { date: '2020-01-01' })).rejects.toThrow(/outside/)
  })
})
