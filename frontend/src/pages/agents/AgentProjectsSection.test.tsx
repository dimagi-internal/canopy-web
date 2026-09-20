// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * The Projects section: what a Drive folder cannot tell you.
 *
 * A `Projects/<name>` folder holds the files. This page answers the questions
 * the folder has no way to answer — how much is open, how much is parked on a
 * person, and where the folder is.
 */

const listAgentProjects = vi.fn()
const listAgentTasks = vi.fn()
const createAgentProject = vi.fn()
const patchAgentProject = vi.fn()

vi.mock('@/api/agents', () => ({
  listAgentProjects,
  listAgentTasks,
  createAgentProject,
  patchAgentProject,
}))

const agent = { slug: 'eva', name: 'Eva' }
vi.mock('react-router-dom', () => ({ useOutletContext: () => ({ agent }) }))

const { AgentProjectsSection } = await import('./AgentProjectsSection')

function project(over: Record<string, unknown> = {}) {
  return {
    id: 1,
    agent_slug: 'eva',
    ext_id: 'P1',
    name: 'UNGA 2026 conference planning',
    outcome: 'Everyone briefed and booked',
    status: 'active',
    owner_note: '',
    owner_email: null,
    drive_folder_id: '',
    drive_folder_url: '',
    repo_slug: '',
    notes: '',
    links: [],
    task_count: 0,
    open_task_count: 0,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    ...over,
  }
}

function task(over: Record<string, unknown> = {}) {
  return {
    id: 10,
    agent_slug: 'eva',
    ext_id: 'T1',
    project_ext_id: 'P1',
    project_name: 'UNGA 2026 conference planning',
    ask_kind: '',
    ask_state: '',
    waiting_on_email: null,
    title: 'Book the room',
    next_action: '',
    status: 'in_progress',
    owner: '',
    assigned: '',
    confidence: '',
    score: '',
    review: '',
    rationale: '',
    source_url: '',
    plan: '',
    due: null,
    links: [],
    notes: '',
    position: 0,
    updated_at: '2026-09-01T00:00:00Z',
    ...over,
  }
}

beforeEach(() => {
  listAgentProjects.mockResolvedValue([])
  listAgentTasks.mockResolvedValue([])
  createAgentProject.mockResolvedValue(project())
  patchAgentProject.mockResolvedValue(project({ status: 'done' }))
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('the projects section', () => {
  it('says what a project IS when there are none', async () => {
    render(<AgentProjectsSection />)

    // Not a bare "nothing here": a reader who has never met the concept needs
    // to know it is the Drive folder they already have.
    expect(await screen.findByText(/Drive folders/)).toBeTruthy()
  })

  it('shows each project with its counts and its Drive folder', async () => {
    listAgentProjects.mockResolvedValue([
      project({ task_count: 3, open_task_count: 1, drive_folder_url: 'https://drive/x' }),
    ])
    render(<AgentProjectsSection />)

    expect(await screen.findByText('UNGA 2026 conference planning')).toBeTruthy()
    expect(screen.getByText('1 open / 3 tasks')).toBeTruthy()
    expect(screen.getByText('Drive folder').getAttribute('href')).toBe('https://drive/x')
  })

  it('lists the tasks filed into that project, and only those', async () => {
    listAgentProjects.mockResolvedValue([project()])
    listAgentTasks.mockResolvedValue([
      task({ id: 1, ext_id: 'T1', title: 'Book the room' }),
      task({ id: 2, ext_id: 'T9', title: 'Unrelated', project_ext_id: null, project_name: null }),
    ])
    render(<AgentProjectsSection />)

    const row = await screen.findByTestId('project-P1')
    expect(row.textContent).toContain('Book the room')
    expect(row.textContent).not.toContain('Unrelated')
  })

  it('counts what is parked on a person — the thing a folder cannot show', async () => {
    listAgentProjects.mockResolvedValue([project()])
    listAgentTasks.mockResolvedValue([
      task({ id: 1, status: 'suggested' }),
      task({ id: 2, ext_id: 'T2', ask_state: 'open', ask_kind: 'question' }),
      task({ id: 3, ext_id: 'T3', status: 'in_progress' }),
    ])
    render(<AgentProjectsSection />)

    expect(await screen.findByText('2 waiting on a person')).toBeTruthy()
  })

  it('says how many tasks are in no project, without nagging', async () => {
    listAgentProjects.mockResolvedValue([project()])
    listAgentTasks.mockResolvedValue([task({ project_ext_id: null, project_name: null })])
    render(<AgentProjectsSection />)

    // One-offs are legitimate: a project per task is what the Drive layout
    // warns against, so this is a count and not a call to action.
    expect(await screen.findByText(/1 task not in a project/)).toBeTruthy()
  })

  it('adds a project and reloads', async () => {
    render(<AgentProjectsSection />)

    fireEvent.change(await screen.findByLabelText('New project name'), {
      target: { value: 'Coefficient EOI' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add project' }))

    await waitFor(() =>
      expect(createAgentProject).toHaveBeenCalledWith('eva', { name: 'Coefficient EOI' }),
    )
    expect(listAgentProjects).toHaveBeenCalledTimes(2)   // initial + after create
  })

  it('will not add an empty project', async () => {
    render(<AgentProjectsSection />)

    fireEvent.click(await screen.findByRole('button', { name: 'Add project' }))

    expect(createAgentProject).not.toHaveBeenCalled()
  })

  it('marks a project done, and offers that only while it is active', async () => {
    listAgentProjects.mockResolvedValue([project({ status: 'done' })])
    const { unmount } = render(<AgentProjectsSection />)
    expect(await screen.findByText('Done')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Mark done' })).toBeNull()
    unmount()

    listAgentProjects.mockResolvedValue([project()])
    render(<AgentProjectsSection />)
    fireEvent.click(await screen.findByRole('button', { name: 'Mark done' }))

    await waitFor(() =>
      expect(patchAgentProject).toHaveBeenCalledWith('eva', 'P1', { status: 'done' }),
    )
  })
})
