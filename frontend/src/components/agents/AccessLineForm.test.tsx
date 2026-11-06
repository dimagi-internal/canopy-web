// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AccessRoleOut } from '@/api/agents'
import { AccessLineForm } from './AccessLineForm'

const role = (key: string, label: string, contains_write: boolean, description = ''): AccessRoleOut => ({
  role: key, label, description, can: [], permissions: [], contains_write, defined_on: 'dimagi', fixed: false,
})
// Strongest first, as the server sends them; owner and admin are fixed and never offered.
const ROLES: AccessRoleOut[] = [
  { ...role('owner', 'Owner', true), fixed: true, defined_on: '' },
  role('pilot', 'Pilot', true, 'flies the agent'),
  role('connector', 'Connect site', false, 'only Connect'),
  role('read_only', 'Read only', false),
]

afterEach(cleanup)

describe('AccessLineForm with roles from the server', () => {
  it('offers the custom role with its description, weakest first, and not the fixed ones', () => {
    render(<AccessLineForm roles={ROLES} workspaces={[['dimagi', 'Dimagi']]} onAdd={vi.fn()} />)
    const names = screen.getAllByRole('radio').map((r) => (r as HTMLInputElement).value)
    expect(names).toEqual(['read_only', 'connector', 'pilot', 'host_site'])
    expect(screen.getByRole('radio', { name: /Connect site/ })).toBeTruthy()
    expect(screen.getByText(/only Connect/)).toBeTruthy()
  })

  it('disables a role that drives for a line that names people only by address, from the server\'s flag', () => {
    render(<AccessLineForm roles={ROLES} workspaces={[['dimagi', 'Dimagi']]} onAdd={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Who'), { target: { value: 'domain' } })
    expect((screen.getByRole('radio', { name: /Pilot/ }) as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByRole('radio', { name: /Connect site/ }) as HTMLInputElement).disabled).toBe(false)
  })

  it('sends the chosen key', async () => {
    const onAdd = vi.fn().mockResolvedValue(undefined)
    render(<AccessLineForm roles={ROLES} workspaces={[['dimagi', 'Dimagi']]} onAdd={onAdd} />)
    fireEvent.change(screen.getByLabelText('Their email'), { target: { value: 'a@dimagi.com' } })
    fireEvent.click(screen.getByRole('radio', { name: /Connect site/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(onAdd).toHaveBeenCalledWith(expect.objectContaining({ kind: 'person', role: 'connector' })))
  })
})
