// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AccessLineOut } from '@/api/agents'

const listAgentDefaults = vi.fn()
const addAgentDefault = vi.fn()
const removeAgentDefault = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listAgentDefaults: (...a: unknown[]) => listAgentDefaults(...a),
  addAgentDefault: (...a: unknown[]) => addAgentDefault(...a),
  removeAgentDefault: (...a: unknown[]) => removeAgentDefault(...a),
}))
vi.mock('@/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ workspaces: [{ slug: 'connect', display_name: 'Connect', role: 'admin' }], active: 'connect', loading: false, refresh: vi.fn() }),
}))

const { WorkspaceAgentDefaults } = await import('./WorkspaceAgentDefaults')

const LINE: AccessLineOut = {
  id: 7, scope: 'workspace', scope_workspace: 'connect', kind: 'workspace', who: 'Everyone in connect', role: 'viewer',
  user_id: null, subject_workspace: 'connect', at_least: '', domain: '', verified: false, manual_only: false, note: '',
  granted_by_email: null, granted_at: null, can_edit: true,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('WorkspaceAgentDefaults', () => {
  it('lists the defaults and lets a workspace admin remove one', async () => {
    listAgentDefaults.mockResolvedValue([LINE])
    removeAgentDefault.mockResolvedValue(undefined)
    render(<WorkspaceAgentDefaults workspace="connect" canManage isOwner={false} />)
    expect((await screen.findByTestId('agent-default')).textContent).toContain('Everyone in connect')
    fireEvent.click(screen.getByRole('button', { name: 'Remove default for Everyone in connect' }))
    await waitFor(() => expect(removeAgentDefault).toHaveBeenCalledWith('connect', 7))
  })

  it('offers editor only to the workspace owner', async () => {
    listAgentDefaults.mockResolvedValue([])
    const { unmount } = render(<WorkspaceAgentDefaults workspace="connect" canManage isOwner={false} />)
    expect(((await screen.findByRole('radio', { name: /editor/i })) as HTMLInputElement).disabled).toBe(true)
    unmount()
    render(<WorkspaceAgentDefaults workspace="connect" canManage isOwner />)
    expect(((await screen.findByRole('radio', { name: /editor/i })) as HTMLInputElement).disabled).toBe(false)
  })

  it('adds a default and shows a refusal inline', async () => {
    listAgentDefaults.mockResolvedValue([])
    addAgentDefault.mockRejectedValueOnce(new Error('changing a workspace\'s default access takes a workspace admin'))
    render(<WorkspaceAgentDefaults workspace="connect" canManage isOwner={false} />)
    fireEvent.change(await screen.findByLabelText('Who'), { target: { value: 'workspace' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add default' }))
    expect((await screen.findByRole('alert')).textContent).toContain('takes a workspace admin')
    addAgentDefault.mockResolvedValueOnce(LINE)
    fireEvent.click(screen.getByRole('button', { name: 'Add default' }))
    await waitFor(() =>
      expect(addAgentDefault).toHaveBeenLastCalledWith('connect', expect.objectContaining({ kind: 'workspace', workspace: 'connect', role: 'user' })),
    )
  })

  it('has no controls for someone who cannot manage members', async () => {
    listAgentDefaults.mockResolvedValue([{ ...LINE, can_edit: false }])
    render(<WorkspaceAgentDefaults workspace="connect" canManage={false} isOwner={false} />)
    await screen.findByTestId('agent-default')
    expect(screen.queryByTestId('access-line-form')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })
})
