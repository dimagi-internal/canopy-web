// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { ApiError } from '@/api/problem'

const getTaskSettings = vi.fn()
const setTaskSettings = vi.fn()
const listLabels = vi.fn()
const createLabel = vi.fn()
const updateLabel = vi.fn()
const deleteLabel = vi.fn()
vi.mock('@/api/work', async (orig) => ({
  ...(await orig<typeof import('@/api/work')>()),
  getTaskSettings: (...a: unknown[]) => getTaskSettings(...a),
  setTaskSettings: (...a: unknown[]) => setTaskSettings(...a),
  listLabels: (...a: unknown[]) => listLabels(...a),
  createLabel: (...a: unknown[]) => createLabel(...a),
  updateLabel: (...a: unknown[]) => updateLabel(...a),
  deleteLabel: (...a: unknown[]) => deleteLabel(...a),
}))
let role = 'editor'
vi.mock('@/workspace/WorkspaceProvider', () => ({
  useWorkspaceRole: () => role,
}))

const { WorkspaceTaskSettingsPage } = await import('./WorkspaceTaskSettingsPage')

const SETTINGS = { workspace: 'connect', prefix: 'CON', status_names: { started: 'Doing' }, update_cadence_days: 7 }

function show() {
  getTaskSettings.mockResolvedValue(SETTINGS)
  listLabels.mockResolvedValue([{ id: 3, workspace: 'connect', name: 'Bug', color: 'destructive', description: '', task_count: 2 }])
  render(
    <MemoryRouter initialEntries={['/w/connect/settings/tasks']}>
      <Routes>
        <Route path="/w/:workspace/settings/tasks" element={<WorkspaceTaskSettingsPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  role = 'editor'
})

describe('WorkspaceTaskSettingsPage', () => {
  it('saves the status names (blank meaning the default) and the cadence, without resending the prefix', async () => {
    setTaskSettings.mockImplementation(async (_ws, body) => ({ ...SETTINGS, ...body }))
    show()
    const started = await screen.findByLabelText('Name for In progress')
    expect((started as HTMLInputElement).value).toBe('Doing')
    fireEvent.change(started, { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('Name for Todo'), { target: { value: 'Ready' } })
    fireEvent.change(screen.getByLabelText('Update cadence in days'), { target: { value: '14' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(setTaskSettings).toHaveBeenCalledTimes(1))
    expect(setTaskSettings).toHaveBeenCalledWith('connect', {
      status_names: { triage: '', backlog: '', unstarted: 'Ready', started: '', completed: '', canceled: '' },
      update_cadence_days: 14,
    })
    expect(await screen.findByText('Saved.')).toBeTruthy()
  })

  it('shows the server’s refusal of a prefix already in use', async () => {
    setTaskSettings.mockRejectedValue(new ApiError(422, 'CON is on 12 tasks already; the prefix can change only before the first task'))
    show()
    const prefix = await screen.findByLabelText('Key prefix')
    fireEvent.change(prefix, { target: { value: 'dim' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(setTaskSettings).toHaveBeenCalledWith('connect', expect.objectContaining({ prefix: 'DIM' })))
    expect((await screen.findByRole('alert')).textContent).toContain('the prefix can change only before the first task')
  })

  it('adds, recolours and renames labels', async () => {
    createLabel.mockResolvedValue({})
    updateLabel.mockResolvedValue({})
    show()
    fireEvent.change(await screen.findByLabelText('New label name'), { target: { value: 'Design' } })
    const newColour = screen.getByRole('radiogroup', { name: 'New label colour' })
    fireEvent.click(newColour.querySelector('[aria-label="info"]') as HTMLElement)
    fireEvent.click(screen.getByRole('button', { name: 'Add label' }))
    await waitFor(() => expect(createLabel).toHaveBeenCalledWith('connect', 'Design', 'info'))

    const colours = screen.getByRole('radiogroup', { name: 'Colour of Bug' })
    fireEvent.click(colours.querySelector('[aria-label="success"]') as HTMLElement)
    await waitFor(() => expect(updateLabel).toHaveBeenCalledWith(3, { color: 'success' }))

    const name = screen.getByLabelText('Rename Bug')
    fireEvent.change(name, { target: { value: 'Defect' } })
    fireEvent.blur(name)
    await waitFor(() => expect(updateLabel).toHaveBeenCalledWith(3, { name: 'Defect' }))
  })

  it('is read-only for someone who is not an editor', async () => {
    role = 'viewer'
    show()
    const prefix = await screen.findByLabelText('Key prefix')
    expect(prefix.matches(':disabled')).toBe(true)
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    expect(screen.queryByLabelText('New label name')).toBeNull()
    expect(screen.getByText(/Only an editor can change these/)).toBeTruthy()
  })
})
