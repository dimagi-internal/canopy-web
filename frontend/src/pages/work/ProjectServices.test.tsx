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
  add_command: 'claude mcp add --transport http salesforce https://mcp.example.org/sf', updated_at: '2026-10-24T00:00:00Z',
}

function detail(over: Record<string, unknown> = {}) {
  return { may_edit: true, services: [mcp], ...over } as never
}

describe('ProjectServices', () => {
  it('lists what the project shares with the line that adds it', () => {
    render(<ProjectServices detail={detail({ may_edit: false })} workspace="dimagi" handle="funders" onChanged={() => {}} />)
    expect(screen.getByTestId('project-services').textContent).toContain(mcp.add_command)
    expect(screen.queryByTestId('add-service')).toBeNull()
    expect(screen.queryByText('Stop sharing')).toBeNull()
  })

  it('shows nothing to a teammate when the project shares nothing', () => {
    const { container } = render(
      <ProjectServices detail={detail({ may_edit: false, services: [] })} workspace="dimagi" handle="funders" onChanged={() => {}} />,
    )
    expect(container.textContent).toBe('')
  })

  it('lets an editor share an MCP server and stop sharing one', async () => {
    const onChanged = vi.fn()
    addProjectService.mockResolvedValue(mcp)
    removeProjectService.mockResolvedValue(undefined)
    render(<ProjectServices detail={detail()} workspace="dimagi" handle="funders" onChanged={onChanged} />)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'github' } })
    fireEvent.change(screen.getByLabelText('URL'), { target: { value: 'https://mcp.example.org/gh' } })
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    await waitFor(() =>
      expect(addProjectService).toHaveBeenCalledWith('dimagi', 'funders', {
        kind: 'mcp', name: 'github', url: 'https://mcp.example.org/gh', description: '',
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Stop sharing' }))
    await waitFor(() => expect(removeProjectService).toHaveBeenCalledWith('dimagi', 'funders', 7))
    expect(onChanged).toHaveBeenCalledTimes(2)
  })
})
