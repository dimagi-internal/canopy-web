// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const addProjectService = vi.fn()
const removeProjectService = vi.fn()
vi.mock('@/api/work', async (orig) => ({
  ...(await orig<typeof import('@/api/work')>()),
  addProjectService: (...a: unknown[]) => addProjectService(...a),
  removeProjectService: (...a: unknown[]) => removeProjectService(...a),
}))

const { ProjectServices } = await import('./ProjectServices')

afterEach(() => {
  cleanup()
  addProjectService.mockReset()
  removeProjectService.mockReset()
})

const mcp = {
  id: 7, kind: 'mcp', name: 'salesforce', description: 'Our CRM', url: 'https://mcp.example.org/sf',
  add_command: 'claude mcp add --transport http salesforce https://mcp.example.org/sf', instructions: '',
  updated_at: '2026-10-24T00:00:00Z',
}
const skill = {
  id: 8, kind: 'skill', name: 'cite-givewell', description: 'Citing a figure', url: '', add_command: '',
  instructions: 'Quote the figure, then link its page.', updated_at: '2026-10-24T00:00:00Z',
}

function detail(over: Record<string, unknown> = {}) {
  return { may_edit: true, services: [mcp, skill], ...over } as never
}

describe('ProjectServices', () => {
  it('says how each thing reaches Claude, and how to open the project', () => {
    render(<ProjectServices detail={detail({ may_edit: false })} workspace="dimagi" handle="funders" onChanged={() => {}} />)
    const table = screen.getByTestId('project-services').textContent
    expect(table).toContain(mcp.add_command)
    expect(table).toContain('Read when the work needs it')
    expect(table).toContain('Quote the figure, then link its page.')
    const how = screen.getByTestId('toolbox-how').textContent
    expect(how).toContain('Nothing here reaches')
    expect(how).toContain('open dimagi/funders')
    expect(how).toContain('claude mcp add --transport http canopy-projects')
    expect(screen.queryByTestId('add-service')).toBeNull()
    expect(screen.queryByText('Remove')).toBeNull()
  })

  it('shows nothing to a teammate when the toolbox is empty', () => {
    const { container } = render(
      <ProjectServices detail={detail({ may_edit: false, services: [] })} workspace="dimagi" handle="funders" onChanged={() => {}} />,
    )
    expect(container.textContent).toBe('')
  })

  it('lets an editor write a skill, add a server and remove one', async () => {
    const onChanged = vi.fn()
    addProjectService.mockResolvedValue(skill)
    removeProjectService.mockResolvedValue(undefined)
    render(<ProjectServices detail={detail()} workspace="dimagi" handle="funders" onChanged={onChanged} />)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'cite' } })
    expect((screen.getByRole('button', { name: 'Add to toolbox' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Instructions'), { target: { value: 'Quote, then link.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add to toolbox' }))
    await waitFor(() =>
      expect(addProjectService).toHaveBeenCalledWith('dimagi', 'funders', {
        kind: 'skill', name: 'cite', url: '', description: '', instructions: 'Quote, then link.',
      }),
    )

    fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'mcp' } })
    expect(screen.queryByLabelText('Instructions')).toBeNull()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'github' } })
    fireEvent.change(screen.getByLabelText('URL'), { target: { value: 'https://mcp.example.org/gh' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add to toolbox' }))
    await waitFor(() =>
      expect(addProjectService).toHaveBeenLastCalledWith('dimagi', 'funders', {
        kind: 'mcp', name: 'github', url: 'https://mcp.example.org/gh', description: '',
      }),
    )
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0])
    await waitFor(() => expect(removeProjectService).toHaveBeenCalledWith('dimagi', 'funders', 7))
    expect(onChanged).toHaveBeenCalledTimes(3)
  })
})
