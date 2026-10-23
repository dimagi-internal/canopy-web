// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

const getCanopyProjects = vi.fn()
const setCanopyProjects = vi.fn()
vi.mock('@/api/canopyProjects', () => ({ getCanopyProjects, setCanopyProjects }))

const { CanopyProjectsPanel } = await import('./CanopyProjectsPanel')

describe('CanopyProjectsPanel', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('says canopy is off everywhere when nothing is chosen', async () => {
    getCanopyProjects.mockResolvedValue({ everything: false, projects: [], available: ['canopy-web'] })
    render(<CanopyProjectsPanel />)
    expect(await screen.findByText(/Canopy is off in every repo/)).toBeTruthy()
    expect(screen.getByTestId('canopy-project-canopy-web')).toBeTruthy()
  })

  it('saves the repos you tick, and one you type in', async () => {
    getCanopyProjects.mockResolvedValue({ everything: false, projects: [], available: ['canopy-web', 'blog'] })
    setCanopyProjects.mockResolvedValue({
      everything: false, projects: ['canopy-web', 'later'], available: ['blog', 'canopy-web', 'later'],
    })
    render(<CanopyProjectsPanel />)
    fireEvent.click(await screen.findByTestId('canopy-project-canopy-web'))
    fireEvent.change(screen.getByTestId('canopy-projects-add'), { target: { value: 'later' } })
    fireEvent.click(screen.getByText('Add'))
    await act(async () => {
      fireEvent.click(screen.getByTestId('canopy-projects-save'))
      await Promise.resolve()
    })
    expect(setCanopyProjects).toHaveBeenCalledWith(false, ['canopy-web', 'later'])
  })

  it('hides the list when every repo is chosen', async () => {
    getCanopyProjects.mockResolvedValue({ everything: true, projects: [], available: ['canopy-web'] })
    render(<CanopyProjectsPanel />)
    await screen.findByTestId('canopy-projects-everything')
    expect(screen.queryByTestId('canopy-project-canopy-web')).toBeNull()
  })
})
