// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { PersonAddress } from '@/api/people'

const listMyAddresses = vi.fn<() => Promise<PersonAddress[]>>()
const addMyAddress = vi.fn<(body: object) => Promise<PersonAddress>>()
const removeMyAddress = vi.fn<(addressId: number) => Promise<void>>()
vi.mock('@/api/people', () => ({ listMyAddresses, addMyAddress, removeMyAddress }))
vi.mock('@/auth/AuthProvider', () => ({
  useAuth: () => ({ status: 'authenticated', user: { email: 'gina@example.com' } }),
}))

const { ContactDetailsPanel } = await import('./ContactDetailsPanel')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ContactDetailsPanel', () => {
  it('lists your addresses and adds a phone', async () => {
    listMyAddresses.mockResolvedValue([{ id: 1, kind: 'email', label: 'work', value: 'gina@employer.org', created_at: null }])
    addMyAddress.mockResolvedValue({ id: 2, kind: 'phone', label: 'mobile', value: '+1 617 555 0100', created_at: null })
    render(<ContactDetailsPanel />)
    expect(await screen.findByText('gina@employer.org')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Label for gina@example.com'), { target: { value: 'mobile' } })
    fireEvent.change(screen.getByLabelText('Kind for gina@example.com'), { target: { value: 'phone' } })
    fireEvent.change(screen.getByLabelText('Address for gina@example.com'), { target: { value: '+1 617 555 0100' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(screen.getByText('+1 617 555 0100')).toBeTruthy())
    expect(addMyAddress).toHaveBeenCalledWith({ kind: 'phone', label: 'mobile', value: '+1 617 555 0100' })
    expect(screen.getByText('gina@employer.org')).toBeTruthy()
  })

  it('removes one', async () => {
    listMyAddresses.mockResolvedValue([{ id: 1, kind: 'email', label: 'work', value: 'gina@employer.org', created_at: null }])
    removeMyAddress.mockResolvedValue()
    render(<ContactDetailsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Remove work email for gina@example.com' }))
    await waitFor(() => expect(screen.queryByText('gina@employer.org')).toBeNull())
    expect(removeMyAddress).toHaveBeenCalledWith(1)
  })
})
