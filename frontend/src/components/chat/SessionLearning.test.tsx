// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'

const at = (path: string) => ({ children }: { children: ReactNode }) => (
  <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>
)

const getMySessionLearning = vi.fn()
const removeMySession = vi.fn()
const restoreMySession = vi.fn()
const declareMySessionProject = vi.fn()
const undeclareMySessionProject = vi.fn()
vi.mock('@/api/learning', () => ({
  getMySessionLearning,
  removeMySession,
  restoreMySession,
  declareMySessionProject,
  undeclareMySessionProject,
}))
const listProjects = vi.fn()
vi.mock('@/api/work', () => ({ listProjects }))

const { SessionLearningPanel, useSessionLearning } = await import('./SessionLearning')

const state = (excluded: boolean, projects: { project: string; name: string; tier: string; how: string }[] = []) => ({
  session_id: 's1',
  excluded,
  projects,
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function Harness({ initial }: { initial: ReturnType<typeof state> }) {
  return (
    <MemoryRouter>
      <SessionLearningPanel sessionId="s1" workspace="acme" state={initial} onChange={onChange} />
    </MemoryRouter>
  )
}
const onChange = vi.fn()

describe('useSessionLearning', () => {
  it('is null for someone not in the session (the route says 404) or not a person (403)', async () => {
    getMySessionLearning.mockRejectedValue(new Error('Session not found'))
    const { result } = renderHook(() => useSessionLearning('s1'), { wrapper: at('/') })
    await waitFor(() => expect(getMySessionLearning).toHaveBeenCalledWith('s1', 'live'))
    expect(result.current[0]).toBeNull()
  })

  it('carries the state for a participant', async () => {
    getMySessionLearning.mockResolvedValue(state(false))
    const { result } = renderHook(() => useSessionLearning('s1'), { wrapper: at('/') })
    await waitFor(() => expect(result.current[0]?.excluded).toBe(false))
  })
})

describe('a preview source', () => {
  it('asks for the preview, shows its banner and changes nothing while it is on', async () => {
    getMySessionLearning.mockResolvedValue(state(false))
    renderHook(() => useSessionLearning('s1'), { wrapper: at('/w/acme/sessions/s1?preview=7') })
    await waitFor(() => expect(getMySessionLearning).toHaveBeenCalledWith('s1', 'preview:7'))
    listProjects.mockResolvedValue([])
    const preview = { ...state(false, [{ project: 'supply', name: 'Supply', tier: 'core', how: 'dispatched' }]),
                      source: { kind: 'preview' as const, id: 7, created_at: null, window: null } }
    render(
      <MemoryRouter initialEntries={['/w/acme/sessions/s1?preview=7']}>
        <SessionLearningPanel sessionId="s1" workspace="acme" state={preview} onChange={onChange} />
      </MemoryRouter>,
    )
    expect(screen.getByTestId('brain-preview-banner').textContent).toContain('Preview: what the brain would do')
    expect((screen.getByRole('button', { name: 'Remove from learning' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Not this project' }) as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('SessionLearningPanel', () => {
  it('removes the session from learning, then lets it back in', async () => {
    listProjects.mockResolvedValue([])
    removeMySession.mockResolvedValue(state(true))
    const { rerender } = render(<Harness initial={state(false)} />)
    expect(screen.getByTestId('session-learning-status').textContent).toBe('Learning may use this session')
    fireEvent.click(screen.getByText('Remove from learning'))
    await waitFor(() => expect(removeMySession).toHaveBeenCalledWith('s1'))
    expect(onChange).toHaveBeenCalledWith(state(true))
    rerender(<Harness initial={state(true)} />)
    expect(screen.getByTestId('session-learning-status').textContent).toBe('Removed from learning')
    restoreMySession.mockResolvedValue(state(false))
    fireEvent.click(screen.getByText('Let learning use this again'))
    await waitFor(() => expect(restoreMySession).toHaveBeenCalledWith('s1'))
  })

  it('rejects a project canopy guessed, takes back a declared call, and ties it to another', async () => {
    listProjects.mockResolvedValue([
      { handle: 'alpha', name: 'Alpha', workspace: 'acme' },
      { handle: 'beta', name: 'Beta', workspace: 'acme' },
    ])
    const links = [
      { project: 'alpha', name: 'Alpha', tier: 'candidate', how: 'signals' },
    ]
    declareMySessionProject.mockResolvedValue(state(false, links))
    render(<Harness initial={state(false, links)} />)
    fireEvent.click(screen.getByText('Not this project'))
    await waitFor(() => expect(declareMySessionProject).toHaveBeenCalledWith('s1', 'alpha', 'rejected'))
    const pick = (await screen.findByLabelText('Tie this session to a project')) as HTMLSelectElement
    expect([...pick.options].map((o) => o.value)).toEqual(['', 'beta'])     // alpha is already linked
    fireEvent.change(pick, { target: { value: 'beta' } })
    fireEvent.click(screen.getByText('Add'))
    await waitFor(() => expect(declareMySessionProject).toHaveBeenCalledWith('s1', 'beta', 'core'))
  })

  it('offers to take back only a declared call', async () => {
    listProjects.mockResolvedValue([])
    undeclareMySessionProject.mockResolvedValue(state(false))
    render(<Harness initial={state(false, [{ project: 'alpha', name: 'Alpha', tier: 'rejected', how: 'declared' }])} />)
    expect(screen.queryByText('Not this project')).toBeNull()
    fireEvent.click(screen.getByText('Take back my call'))
    await waitFor(() => expect(undeclareMySessionProject).toHaveBeenCalledWith('s1', 'alpha'))
  })
})
