// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { MemberAddresses, PersonAddress } from '@/api/people'

const listWorkspaceAddresses = vi.fn<(ws: string) => Promise<MemberAddresses[]>>()
const addPersonAddress = vi.fn<(personId: number, body: object) => Promise<PersonAddress>>()
const removePersonAddress = vi.fn<(personId: number, addressId: number) => Promise<void>>()
vi.mock('@/api/people', () => ({ listWorkspaceAddresses, addPersonAddress, removePersonAddress }))

const { ContactDetailsSection } = await import('./ContactDetailsSection')

const work: PersonAddress = {
  id: 9, kind: 'email', label: 'work', value: 'gina@employer.org', workspace: 'family',
  added_by: 'jj@example.com', created_at: null,
}

function gina(over: Partial<MemberAddresses> = {}): MemberAddresses {
  return { person_id: 69, user_id: 57, email: 'gina@example.com', display_name: 'Gina', may_edit: true,
           addresses: [], ...over }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ContactDetailsSection', () => {
  it('adds a work email for a member', async () => {
    listWorkspaceAddresses.mockResolvedValue([gina()])
    addPersonAddress.mockResolvedValue(work)
    render(<ContactDetailsSection slug="family" />)
    const input = await screen.findByLabelText('Address for gina@example.com')
    fireEvent.change(input, { target: { value: 'gina@employer.org' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(screen.getByText('gina@employer.org')).toBeTruthy())
    expect(addPersonAddress).toHaveBeenCalledWith(69, {
      workspace: 'family', kind: 'email', label: 'work', value: 'gina@employer.org',
    })
  })

  it('removes one', async () => {
    listWorkspaceAddresses.mockResolvedValue([gina({ addresses: [work] })])
    removePersonAddress.mockResolvedValue()
    render(<ContactDetailsSection slug="family" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Remove work email for gina@example.com' }))
    await waitFor(() => expect(screen.queryByText('gina@employer.org')).toBeNull())
    expect(removePersonAddress).toHaveBeenCalledWith(69, 9)
  })

  it('shows no controls for someone you may not edit', async () => {
    listWorkspaceAddresses.mockResolvedValue([gina({ may_edit: false, addresses: [work] })])
    render(<ContactDetailsSection slug="family" />)
    expect(await screen.findByText('gina@employer.org')).toBeTruthy()
    expect(screen.queryByLabelText('Address for gina@example.com')).toBeNull()
    expect(screen.queryByRole('button', { name: /Remove/ })).toBeNull()
  })
})
