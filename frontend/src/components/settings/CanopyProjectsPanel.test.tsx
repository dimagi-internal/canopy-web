// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

const getCanopyProjects = vi.fn()
const setCanopyProjects = vi.fn()
vi.mock('@/api/canopyProjects', () => ({ getCanopyProjects, setCanopyProjects }))

const { CanopyProjectsPanel } = await import('./CanopyProjectsPanel')

const projects = (over: object) => ({
  repo_workspaces: {}, default_workspaces: {},
  workspaces: [{ slug: 'dimagi', display_name: 'Dimagi' }, { slug: 'family', display_name: 'Family' }],
  ...over,
})

describe('CanopyProjectsPanel', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('says Saved after a save, until you change something again', async () => {
    const loaded = projects({ everything: true, projects: [], available: ['canopy-web'] })
    getCanopyProjects.mockResolvedValue(loaded)
    setCanopyProjects.mockResolvedValue(projects({ everything: false, projects: [], available: ['canopy-web'] }))
    render(<CanopyProjectsPanel />)
    fireEvent.click(await screen.findByTestId('canopy-projects-chosen'))
    expect(screen.queryByText('Saved.')).toBeNull()
    await act(async () => {
      fireEvent.click(screen.getByTestId('canopy-projects-save'))
      await Promise.resolve()
    })
    expect(await screen.findByText('Saved.')).toBeTruthy()
    fireEvent.click(screen.getByTestId('canopy-projects-everything'))
    expect(screen.queryByText('Saved.')).toBeNull()
  })

  it('says nothing was saved when the save fails', async () => {
    getCanopyProjects.mockResolvedValue(projects({ everything: true, projects: [], available: ['canopy-web'] }))
    setCanopyProjects.mockRejectedValue(new Error('no'))
    render(<CanopyProjectsPanel />)
    fireEvent.click(await screen.findByTestId('canopy-projects-chosen'))
    await act(async () => {
      fireEvent.click(screen.getByTestId('canopy-projects-save'))
      await Promise.resolve()
    })
    expect(await screen.findByText('no')).toBeTruthy()
    expect(screen.queryByText('Saved.')).toBeNull()
  })

  it('says canopy is off everywhere when nothing is chosen', async () => {
    getCanopyProjects.mockResolvedValue(projects({ everything: false, projects: [], available: ['canopy-web'] }))
    render(<CanopyProjectsPanel />)
    expect(await screen.findByText(/Canopy is off in every repo/)).toBeTruthy()
    expect(screen.getByTestId('canopy-project-canopy-web')).toBeTruthy()
  })

  it('saves the repos you tick, and one you type in', async () => {
    getCanopyProjects.mockResolvedValue(projects({ everything: false, projects: [], available: ['canopy-web', 'blog'] }))
    setCanopyProjects.mockResolvedValue(projects({
      everything: false, projects: ['canopy-web', 'later'], available: ['blog', 'canopy-web', 'later'],
    }))
    render(<CanopyProjectsPanel />)
    fireEvent.click(await screen.findByTestId('canopy-project-canopy-web'))
    fireEvent.change(screen.getByTestId('canopy-projects-add'), { target: { value: 'later' } })
    fireEvent.click(screen.getByText('Add'))
    await act(async () => {
      fireEvent.click(screen.getByTestId('canopy-projects-save'))
      await Promise.resolve()
    })
    expect(setCanopyProjects).toHaveBeenCalledWith(false, ['canopy-web', 'later'], {})
  })

  it('drops the tick boxes but keeps each repo\'s workspaces when every repo is chosen', async () => {
    getCanopyProjects.mockResolvedValue(projects({
      everything: true, projects: [], available: ['canopy-web'], default_workspaces: { 'canopy-web': ['dimagi'] },
    }))
    render(<CanopyProjectsPanel />)
    await screen.findByTestId('canopy-projects-everything')
    expect(screen.queryByTestId('canopy-project-canopy-web')).toBeNull()
    expect(screen.getByTestId('canopy-project-workspaces-canopy-web').textContent).toBe('dimagi')
  })

  it('assigns a repo to a workspace, leaving the others on their default', async () => {
    getCanopyProjects.mockResolvedValue(projects({
      everything: true, projects: [], available: ['bailey', 'canopy-web'],
      default_workspaces: { bailey: ['dimagi'], 'canopy-web': ['dimagi'] },
    }))
    setCanopyProjects.mockResolvedValue(projects({
      everything: true, projects: [], available: ['bailey', 'canopy-web'],
      repo_workspaces: { bailey: ['dimagi', 'family'] }, default_workspaces: { bailey: ['dimagi'], 'canopy-web': ['dimagi'] },
    }))
    render(<CanopyProjectsPanel />)
    fireEvent.click(await screen.findByTestId('canopy-project-workspaces-bailey'))
    fireEvent.click(await screen.findByTestId('canopy-project-workspace-bailey-family'))
    expect(screen.getByTestId('canopy-project-workspaces-bailey').textContent).toBe('dimagi, family')
    await act(async () => {
      fireEvent.click(screen.getByTestId('canopy-projects-save'))
      await Promise.resolve()
    })
    expect(setCanopyProjects).toHaveBeenCalledWith(true, [], { bailey: ['dimagi', 'family'] })
  })
})
