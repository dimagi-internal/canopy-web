// @vitest-environment jsdom
//
// Projects is a destination again: one row per project, active first, the
// finished ones folded away, and "Add project" refuses a project with no outcome
// — a project without "what done looks like" is a task.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const { listProjects, createProject, ctx } = vi.hoisted(() => ({
  listProjects: vi.fn<(slug: string, status?: string) => Promise<unknown[]>>(async () => []),
  createProject: vi.fn(async () => ({})),
  ctx: { canEdit: true, waiting: 0, refreshWaiting: () => {} },
}))

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listProjects,
  createProject,
}))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({ agent: { slug: 'eva', name: 'Eva' }, ...ctx }),
}))

const { AgentProjectsSection } = await import('./AgentProjectsSection')

function project(over: Record<string, unknown> = {}) {
  return {
    id: 1, agent_slug: 'eva', ext_id: 'P1', name: 'Connect Enterprise',
    outcome: 'A clear "what" explanation of Connect Enterprise',
    status: 'active', owner_note: '', owner_email: 'jonathan@dimagi.com',
    drive_folder_id: '', drive_folder_url: '', repo_slug: '', notes: '', links: [],
    task_count: 4, open_task_count: 3, waiting_task_count: 1,
    created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-05T00:00:00Z', ...over,
  }
}

function show() {
  return render(
    <MemoryRouter initialEntries={['/w/connect/agents/eva/projects']}>
      <Routes>
        <Route path="/w/:workspace/agents/:slug/projects" element={<AgentProjectsSection />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  listProjects.mockResolvedValue([])
  ctx.canEdit = true
})

describe('AgentProjectsSection', () => {
  it('lists active projects as rows and folds done/archived into a collapsed group', async () => {
    listProjects.mockResolvedValue([
      project(),
      project({ id: 2, ext_id: 'P2', name: 'Old launch', status: 'done' }),
      project({ id: 3, ext_id: 'P3', name: 'Shelved idea', status: 'archived' }),
    ])
    show()
    const row = await screen.findByTestId('project-row-P1')
    expect(row.getAttribute('href')).toBe('/w/connect/agents/eva/projects/P1')
    expect(row.textContent).toContain('Connect Enterprise')
    expect(row.textContent).toContain('jonathan@dimagi.com')
    expect(row.textContent).toContain('3 open · 1 waiting')
    expect(row.textContent).toContain('A clear "what" explanation')

    const group = screen.getByTestId('projects-finished') as HTMLDetailsElement
    expect(group.open).toBe(false)
    expect(group.textContent).toContain('Done and archived (2)')
    expect(group.contains(screen.getByTestId('project-row-P2'))).toBe(true)
    expect(group.contains(screen.getByTestId('project-row-P3'))).toBe(true)
    expect(group.contains(row)).toBe(false)
  })

  it('shows the empty state when the agent has no projects', async () => {
    show()
    expect(await screen.findByText(/No projects yet\. A project is a piece of work with an end/)).toBeTruthy()
  })

  it('Add project needs both a name and an outcome, then creates and reloads', async () => {
    show()
    await waitFor(() => expect(listProjects).toHaveBeenCalledTimes(1))
    const add = screen.getByRole('button', { name: 'Add project' }) as HTMLButtonElement
    expect(add.disabled).toBe(true)

    fireEvent.change(screen.getByLabelText('New project name'), { target: { value: 'UNGA 2026' } })
    expect(add.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('New project outcome'), {
      target: { value: 'Every meeting booked and briefed' },
    })
    expect(add.disabled).toBe(false)

    fireEvent.click(add)
    await waitFor(() => expect(createProject).toHaveBeenCalledWith('eva', {
      name: 'UNGA 2026', outcome: 'Every meeting booked and briefed',
    }))
    await waitFor(() => expect(listProjects).toHaveBeenCalledTimes(2))
  })

  it('a viewer cannot add a project', async () => {
    ctx.canEdit = false
    show()
    await waitFor(() => expect(listProjects).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByLabelText('New project name'), { target: { value: 'X' } })
    fireEvent.change(screen.getByLabelText('New project outcome'), { target: { value: 'Y' } })
    const add = screen.getByRole('button', { name: 'Add project' }) as HTMLButtonElement
    expect(add.disabled).toBe(true)
  })
})
