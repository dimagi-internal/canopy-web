// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const listProjectShares = vi.fn()
const addProjectShare = vi.fn()
const removeProjectShare = vi.fn()
const listWorkspaces = vi.fn()
vi.mock('@/api/work', async (orig) => ({
  ...(await orig<typeof import('@/api/work')>()),
  listProjectShares: (...a: unknown[]) => listProjectShares(...a),
  addProjectShare: (...a: unknown[]) => addProjectShare(...a),
  removeProjectShare: (...a: unknown[]) => removeProjectShare(...a),
}))
vi.mock('@/api/workspaces', async (orig) => ({
  ...(await orig<typeof import('@/api/workspaces')>()),
  listWorkspaces: (...a: unknown[]) => listWorkspaces(...a),
}))

const { ProjectShares } = await import('./ProjectShares')

afterEach(() => {
  cleanup()
  for (const m of [listProjectShares, addProjectShare, removeProjectShare, listWorkspaces]) m.mockReset()
})

const ws = (slug: string, parent: string | null, root: string) => ({ slug, display_name: slug.toUpperCase(), parent, root })
const TREE = [
  ws('dimagi', null, 'dimagi'),
  ws('connect', 'dimagi', 'dimagi'),
  ws('labs', 'dimagi', 'dimagi'),
  ws('outsider', null, 'outsider'),
]
const share = { workspace: 'connect', added_by: 'jj@dimagi.com', created_at: '2026-10-30T00:00:00Z', member_agents: ['hal'] }

describe('ProjectShares', () => {
  it('lists who it is shared with and the agents that joined through each', async () => {
    listProjectShares.mockResolvedValue([share])
    render(<ProjectShares detail={{ may_edit: false } as never} workspace="dimagi" handle="canopy-support" onChanged={() => {}} />)
    const list = await screen.findByTestId('project-shares')
    expect(list.textContent).toContain('connect')
    expect(list.textContent).toContain('Agents on it: hal')
    expect(screen.queryByTestId('add-share')).toBeNull()
    expect(screen.queryByText('Stop sharing')).toBeNull()
  })

  it('shows a teammate nothing when it is not shared', async () => {
    listProjectShares.mockResolvedValue([])
    const { container } = render(
      <ProjectShares detail={{ may_edit: false } as never} workspace="dimagi" handle="x" onChanged={() => {}} />,
    )
    await waitFor(() => expect(listProjectShares).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('offers only workspaces of its own tree that it does not reach already', async () => {
    listProjectShares.mockResolvedValue([])
    listWorkspaces.mockResolvedValue(TREE)
    render(<ProjectShares detail={{ may_edit: true } as never} workspace="dimagi" handle="x" onChanged={() => {}} />)
    const select = (await screen.findByLabelText('Workspace')) as HTMLSelectElement
    await waitFor(() => expect(select.options.length).toBe(3))
    expect([...select.options].map((o) => o.value)).toEqual(['', 'connect', 'labs'])
  })

  it('shares with the picked workspace, and says who a revoke removed', async () => {
    const changed = vi.fn()
    listProjectShares.mockResolvedValue([share])
    listWorkspaces.mockResolvedValue(TREE)
    addProjectShare.mockResolvedValue({ ...share, workspace: 'labs', member_agents: [] })
    removeProjectShare.mockResolvedValue({ workspace: 'connect', removed_agents: ['hal'] })
    render(<ProjectShares detail={{ may_edit: true } as never} workspace="dimagi" handle="x" onChanged={changed} />)
    const select = (await screen.findByLabelText('Workspace')) as HTMLSelectElement
    await waitFor(() => expect(select.options.length).toBe(2))
    fireEvent.change(select, { target: { value: 'labs' } })
    fireEvent.click(screen.getByText('Share'))
    await waitFor(() => expect(addProjectShare).toHaveBeenCalledWith('dimagi', 'x', 'labs'))

    fireEvent.click(await screen.findByText('Stop sharing'))
    await waitFor(() => expect(removeProjectShare).toHaveBeenCalledWith('dimagi', 'x', 'connect'))
    expect((await screen.findByText(/Removed from the project: hal/)).textContent).toContain('hal')
    expect(changed).toHaveBeenCalled()
  })
})
