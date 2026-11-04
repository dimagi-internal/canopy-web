// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentRoleOut, AgentRolesOut } from '@/api/agents'

const { listAgentRoles, createAgentRole, changeAgentRole, deleteAgentRole, resetAgentRole } = vi.hoisted(() => ({
  listAgentRoles: vi.fn(),
  createAgentRole: vi.fn(),
  changeAgentRole: vi.fn(),
  deleteAgentRole: vi.fn(),
  resetAgentRole: vi.fn(),
}))
vi.mock('@/api/agents', () => ({ listAgentRoles, createAgentRole, changeAgentRole, deleteAgentRole, resetAgentRole }))

import { WorkspaceAgentRolesPage } from './WorkspaceAgentRolesPage'

const role = (key: string, label: string, permissions: string[], over: Partial<AgentRoleOut> = {}): AgentRoleOut => ({
  key, label, description: '', permissions, can: [], defined_on: 'dimagi', builtin: false, contains_write: false,
  in_use: { lines: 0, agents: 0, defaults: 0 }, can_edit: true, ...over,
})

function catalog(over: Partial<AgentRolesOut> = {}): AgentRolesOut {
  return {
    roles: [
      role('editor', 'Editor', ['read', 'write', 'agent_edit'], { builtin: true, contains_write: true, can: ['drive the whole agent', 'change its skills and capabilities'] }),
      role('connector', 'Connect door', ['read', 'use:connect'], { can: ['be answered in read mode', "use the 'connect' door"], in_use: { lines: 3, agents: 2, defaults: 1 } }),
      role('read_only', 'Read only', ['read'], { builtin: true, can: ['be answered in read mode'] }),
    ],
    fixed: [{ key: 'owner', label: 'Owner', can: ['transfer or delete it'], why: 'whoever owns the agent' },
      { key: 'admin', label: 'Admin', can: ['configure it'], why: 'people who hold the keys' }],
    can_edit: true,
    can_give_write: false,
    ...over,
  }
}

function show() {
  return render(
    <MemoryRouter initialEntries={['/w/dimagi/settings/roles']}>
      <Routes>
        <Route path="/w/:workspace/settings/roles" element={<WorkspaceAgentRolesPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('WorkspaceAgentRolesPage', () => {
  it('lists the roles in words with where they are used, and the fixed ones read-only', async () => {
    listAgentRoles.mockResolvedValue(catalog())
    show()
    const row = await screen.findByTestId('agent-role-connector')
    expect(row.textContent).toContain("Can be answered in read mode, use the 'connect' door")
    expect(row.textContent).toContain('3 lines · 2 agents · 1 default')
    expect(screen.getByTestId('agent-role-editor').textContent).toContain('built in')
    expect(screen.getByTestId('agent-role-fixed-owner').textContent).toContain('Not a line')
    expect(screen.queryByRole('button', { name: 'Delete Read only' })).toBeNull()      // a built-in is never deletable
  })

  it('defines a role: the key follows the name, doors become use: entries', async () => {
    listAgentRoles.mockResolvedValue(catalog())
    createAgentRole.mockResolvedValue(role('reviewer', 'Reviewer', ['read', 'use:report']))
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'New role' }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Reviewer' } })
    expect((screen.getByLabelText('Key') as HTMLInputElement).value).toBe('reviewer')
    fireEvent.click(screen.getByLabelText(/Be answered in read mode/))
    fireEvent.change(screen.getByLabelText('Doors'), { target: { value: 'report' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create role' }))
    await waitFor(() => expect(createAgentRole).toHaveBeenCalledWith('dimagi', {
      key: 'reviewer', label: 'Reviewer', description: '', permissions: ['read', 'use:report'],
    }))
    expect(listAgentRoles).toHaveBeenCalledTimes(2)
  })

  it('offers driving and changing the agent only to the workspace owner, and ticking one ticks what it includes', async () => {
    listAgentRoles.mockResolvedValue(catalog())
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'New role' }))
    expect((screen.getByLabelText(/Drive the whole agent/) as HTMLInputElement).disabled).toBe(true)
    cleanup()

    listAgentRoles.mockResolvedValue(catalog({ can_give_write: true }))
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'New role' }))
    fireEvent.click(screen.getByLabelText(/Change its skills and capabilities/))
    expect((screen.getByLabelText(/Drive the whole agent/) as HTMLInputElement).checked).toBe(true)
    expect((screen.getByLabelText(/Be answered in read mode/) as HTMLInputElement).checked).toBe(true)
  })

  it('shows the server\'s refusal beside the form', async () => {
    listAgentRoles.mockResolvedValue(catalog())
    changeAgentRole.mockRejectedValue(new Error('1 line(s) that name people only by their address hold connector'))
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Edit Connect door' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save role' }))
    expect((await screen.findByRole('alert')).textContent).toContain('only by their address')
  })

  it('copies a role into a new one and moves holders when deleting', async () => {
    listAgentRoles.mockResolvedValue(catalog())
    deleteAgentRole.mockResolvedValue(undefined)
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Copy Connect door' }))
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('Connect door (copy)')
    expect((screen.getByLabelText('Key') as HTMLInputElement).disabled).toBe(false)
    expect((screen.getByLabelText('Doors') as HTMLInputElement).value).toBe('connect')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    fireEvent.click(screen.getByRole('button', { name: 'Delete Connect door' }))
    fireEvent.change(screen.getByLabelText('Move its holders to'), { target: { value: 'read_only' } })
    fireEvent.click(screen.getByRole('button', { name: 'Delete role' }))
    await waitFor(() => expect(deleteAgentRole).toHaveBeenCalledWith('dimagi', 'connector', 'read_only'))
  })

  it('resets a built-in and hides every write from a member', async () => {
    listAgentRoles.mockResolvedValue(catalog())
    resetAgentRole.mockResolvedValue(role('read_only', 'Read only', ['read']))
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Reset Read only to its defaults' }))
    await waitFor(() => expect(resetAgentRole).toHaveBeenCalledWith('dimagi', 'read_only'))
    cleanup()

    listAgentRoles.mockResolvedValue(catalog({ can_edit: false }))
    show()
    await screen.findByTestId('agent-roles')
    expect(screen.queryByRole('button', { name: 'New role' })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Edit / })).toBeNull()
  })
})
