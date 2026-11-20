// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render as rtlRender, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ReactElement } from 'react'

const render = (ui: ReactElement) => rtlRender(<MemoryRouter>{ui}</MemoryRouter>)

const getMyLearning = vi.fn()
const setMyLearning = vi.fn()
const endMyLearning = vi.fn()
const listMyRemovedSessions = vi.fn()
const getMyDisclosure = vi.fn()
const listReporting = vi.fn()
const restoreMySession = vi.fn()
vi.mock('@/api/learning', () => ({
  restoreMySession,
  getMyLearning,
  setMyLearning,
  endMyLearning,
  listMyRemovedSessions,
  getMyDisclosure,
  listReporting,
}))
vi.mock('@/auth/AuthProvider', () => ({ useAuth: () => ({ status: 'authenticated', user: { user_id: 1 } }) }))
vi.mock('@/api/workspaces', () => ({ listWorkspaces: vi.fn().mockResolvedValue([{ slug: 'dimagi' }]) }))

const { LearningPanel } = await import('./LearningPanel')

const klass = (name: string, on: boolean) => ({
  name,
  on,
  learns_about: 'you',
  summary: `${name} summary`,
  seen_by: 'someone',
})
const base = (on: string[]) => ({
  on: on.length > 0,
  since: null,
  classes: ['individual', 'memory', 'project', 'system'].map((n) => klass(n, on.includes(n))),
  workspaces: [],
  include_new: true,
  standing_rules: [],
  directives: [],
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('LearningPanel', () => {
  it('shows the four classes, changes one, and ends it all when the last is cleared', async () => {
    getMyLearning.mockResolvedValue(base(['individual']))
    listMyRemovedSessions.mockResolvedValue([{ session_id: 's1', title: 'A removed chat' }])
    getMyDisclosure.mockResolvedValue([{ day: '2026-11-20', learner: 'project-readers', reads: 3, sources: 2 }])
    listReporting.mockResolvedValue([])
    setMyLearning.mockResolvedValue(base(['individual', 'project']))
    endMyLearning.mockResolvedValue(base([]))
    render(<LearningPanel />)
    const project = (await screen.findByRole('checkbox', { name: 'Allow project learning' })) as HTMLInputElement
    expect(project.checked).toBe(false)
    expect(screen.getByText('A removed chat')).toBeTruthy()
    expect(screen.getByText('project-readers')).toBeTruthy()
    fireEvent.click(project)
    await waitFor(() =>
      // only the classes: the scope stays as the server has it
      expect(setMyLearning).toHaveBeenCalledWith({ classes: ['individual', 'project'] }),
    )
    await waitFor(() =>
      expect((screen.getByRole('checkbox', { name: 'Allow project learning' }) as HTMLInputElement).checked).toBe(true),
    )
    setMyLearning.mockResolvedValue(base(['individual']))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Allow project learning' }))
    await waitFor(() =>
      expect((screen.getByRole('checkbox', { name: 'Allow project learning' }) as HTMLInputElement).checked).toBe(false),
    )
    fireEvent.click(screen.getByRole('checkbox', { name: 'Allow individual learning' }))
    await waitFor(() => expect(endMyLearning).toHaveBeenCalled())
  })

  it('tells a person who is managed that their manager sees individual outputs, with a one-click removal', async () => {
    getMyLearning.mockResolvedValue(base(['individual']))
    listMyRemovedSessions.mockResolvedValue([])
    getMyDisclosure.mockResolvedValue([])
    listReporting.mockResolvedValue([
      { person_id: 1, person: 'Me', manager_id: 2, manager: 'Boss', since: '2026-11-01T00:00:00Z' },
    ])
    endMyLearning.mockResolvedValue(base([]))
    render(<LearningPanel />)
    expect(await screen.findByText(/a manager sees the outputs of coaching/)).toBeTruthy()
    fireEvent.click(screen.getByText('Remove individual'))
    await waitFor(() => expect(endMyLearning).toHaveBeenCalled())
  })

  it('does not tell an owner who manages others that they are managed', async () => {
    getMyLearning.mockResolvedValue(base(['individual']))
    listMyRemovedSessions.mockResolvedValue([])
    getMyDisclosure.mockResolvedValue([])
    listReporting.mockResolvedValue([
      { person_id: 7, person: 'Someone', manager_id: 8, manager: 'Their boss', since: '2026-11-01T00:00:00Z' },
      { person_id: 9, person: 'Report', manager_id: 1, manager: 'Me', since: '2026-11-01T00:00:00Z' },
    ])
    render(<LearningPanel />)
    expect(await screen.findByText('Nobody is set as your manager in your workspaces.')).toBeTruthy()
    expect(screen.queryByText(/a manager sees the outputs of coaching/)).toBeNull()
    expect(screen.queryByText(/Their boss/)).toBeNull()
  })

  it('lists the sessions you took out, each linked, and lets one back in', async () => {
    getMyLearning.mockResolvedValue(base(['individual']))
    listMyRemovedSessions.mockResolvedValue([{ session_id: 's1', title: 'A removed chat', workspace: 'dimagi' }])
    getMyDisclosure.mockResolvedValue([])
    listReporting.mockResolvedValue([])
    restoreMySession.mockResolvedValue({ session_id: 's1', excluded: false, projects: [] })
    render(<LearningPanel />)
    const link = (await screen.findByText('A removed chat')).closest('a')
    expect(link?.getAttribute('href')).toBe('/w/dimagi/sessions/s1')
    fireEvent.click(screen.getByText('Let learning use this again'))
    await waitFor(() => expect(restoreMySession).toHaveBeenCalledWith('s1'))
    await waitFor(() => expect(screen.queryByText('A removed chat')).toBeNull())
  })
})
