// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import type { MemberAddresses, PersonAddress } from '@/api/people'

const listWorkspaceAddresses = vi.fn<(ws: string) => Promise<MemberAddresses[]>>()
const addMyAddress = vi.fn<(body: object) => Promise<PersonAddress>>()
const removeMyAddress = vi.fn<(addressId: number) => Promise<void>>()
vi.mock('@/api/people', () => ({ listWorkspaceAddresses, addMyAddress, removeMyAddress }))

const { ContactDetailsSection } = await import('./ContactDetailsSection')

const work: PersonAddress = { id: 9, kind: 'email', label: 'work', value: 'gina@employer.org', created_at: null }

function gina(over: Partial<MemberAddresses> = {}): MemberAddresses {
  return { person_id: 69, user_id: 57, email: 'gina@example.com', display_name: 'Gina', may_edit: true,
           addresses: [], ...over }
}

function renderSection() {
  render(<MemoryRouter><ContactDetailsSection slug="family" /></MemoryRouter>)
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ContactDetailsSection', () => {
  it('adds a work email on your own card', async () => {
    listWorkspaceAddresses.mockResolvedValue([gina()])
    addMyAddress.mockResolvedValue(work)
    renderSection()
    const input = await screen.findByLabelText('Address for gina@example.com')
    fireEvent.change(input, { target: { value: 'gina@employer.org' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(screen.getByText('gina@employer.org')).toBeTruthy())
    expect(addMyAddress).toHaveBeenCalledWith({ kind: 'email', label: 'work', value: 'gina@employer.org' })
  })

  it('removes one of yours', async () => {
    listWorkspaceAddresses.mockResolvedValue([gina({ addresses: [work] })])
    removeMyAddress.mockResolvedValue()
    renderSection()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove work email for gina@example.com' }))
    await waitFor(() => expect(screen.queryByText('gina@employer.org')).toBeNull())
    expect(removeMyAddress).toHaveBeenCalledWith(9)
  })

  it("shows someone else's read-only", async () => {
    listWorkspaceAddresses.mockResolvedValue([gina({ may_edit: false, addresses: [work] })])
    renderSection()
    expect(await screen.findByText('gina@employer.org')).toBeTruthy()
    expect(screen.queryByLabelText('Address for gina@example.com')).toBeNull()
    expect(screen.queryByRole('button', { name: /Remove/ })).toBeNull()
  })
})
