// @vitest-environment jsdom
//
// A runner's Repos section: what emdash projects the laptop holds, each ask to
// add one and how it went (the box's reason when it failed), and the form its
// owner or an administrator uses to ask.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const listRunnerProjects = vi.fn()
const addRunnerProject = vi.fn()
const deleteRunnerProjectRequest = vi.fn()
vi.mock('@/api/harness', () => ({ listRunnerProjects, addRunnerProject, deleteRunnerProjectRequest }))

const { RunnerProjects } = await import('./RunnerProjects')

const failed = {
  id: 'q1', runner_id: 'r1', name: 'bailey', repo_url: 'https://github.com/marshellis/bailey', agent: 'bailey',
  status: 'failed', detail: 'git clone failed: Repository not found', requested_at: '2026-10-24T00:00:00Z',
}
const agents = [
  { slug: 'ada', name: 'Ada' },
  { slug: 'bailey', name: 'Bailey' },
] as never[]

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerProjects', () => {
  it('shows a failed ask with the box’s reason and retries it as the agent’s repo', async () => {
    listRunnerProjects.mockResolvedValue({ projects: ['ada'], requests: [failed], can_add: true })
    addRunnerProject.mockResolvedValue({ ...failed, status: 'pending', detail: '' })
    render(<RunnerProjects runnerId="r1" agents={agents} />)
    expect((await screen.findByTestId('runner-project-bailey')).textContent).toContain('Repository not found')
    expect(screen.getByTestId('runner-project-status-bailey').textContent).toBe('failed')
    fireEvent.click(screen.getByText('Retry'))
    await waitFor(() => expect(addRunnerProject).toHaveBeenCalledWith('r1', { agent: 'bailey' }))
  })

  it('offers only agents it lacks, and any repo by URL', async () => {
    listRunnerProjects.mockResolvedValue({ projects: ['ada'], requests: [], can_add: true })
    addRunnerProject.mockResolvedValue(failed)
    render(<RunnerProjects runnerId="r1" agents={agents} />)
    const select = (await screen.findByTestId('runner-projects-agent')) as HTMLSelectElement
    expect([...select.options].map((o) => o.value)).toEqual(['', 'bailey'])
    fireEvent.change(screen.getByTestId('runner-projects-url'), { target: { value: ' https://github.com/x/tool ' } })
    fireEvent.click(screen.getByTestId('runner-projects-add-url'))
    await waitFor(() =>
      expect(addRunnerProject).toHaveBeenCalledWith('r1', { repoUrl: 'https://github.com/x/tool', name: '' }))
  })

  it('shows no form to someone who may not ask', async () => {
    listRunnerProjects.mockResolvedValue({ projects: ['ada'], requests: [], can_add: false })
    render(<RunnerProjects runnerId="r1" agents={agents} />)
    await screen.findByTestId('runner-projects')
    expect(screen.queryByTestId('runner-projects-url')).toBeNull()
  })
})
