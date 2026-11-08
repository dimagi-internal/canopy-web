// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { HcpClientRow } from '@/api/people'

const listHcpClients = vi.fn<() => Promise<HcpClientRow[]>>()
const listHcpScopes = vi.fn<() => Promise<string[]>>()
const registerHcpClient = vi.fn()
const updateHcpClient = vi.fn()

vi.mock('@/api/people', () => ({
  MY_CATEGORIES: [
    { value: 'general_preferences', label: 'How I like to work' },
    { value: 'work_context', label: 'My work and role' },
  ],
  listHcpClients,
  listHcpScopes,
  registerHcpClient,
  updateHcpClient,
}))

const { HcpClientsPage } = await import('./HcpClientsPage')

const READ = 'hcp:general_preferences:read'
const WRITE = 'hcp:general_preferences:write'
const WORK = 'hcp:work_context:read'

function client(over: Partial<HcpClientRow> = {}): HcpClientRow {
  return {
    client_id: 'hcpc_1',
    name: 'Recipe Planner',
    operator: 'Example Co',
    description: '',
    redirect_uris: ['https://app.example.org/cb'],
    allowed_scopes: [READ],
    first_party: false,
    webhook_url: '',
    created_at: '2026-11-01T00:00:00Z',
    disabled_at: null,
    active_grants: 2,
    ...over,
  }
}

beforeEach(() => {
  listHcpClients.mockResolvedValue([client()])
  listHcpScopes.mockResolvedValue([READ, WRITE, WORK])
  updateHcpClient.mockResolvedValue(client())
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('HcpClientsPage allowed scopes', () => {
  it('offers exactly the scopes the service publishes when registering', async () => {
    listHcpScopes.mockResolvedValue([READ, 'hcp:coordination_context:write'])
    render(<HcpClientsPage />)
    const form = await screen.findByRole('form', { name: 'Register a client' })
    const boxes = within(form).getAllByRole('checkbox')
    // two scopes + the "Operated by Dimagi" box: nothing the service did not name
    expect(boxes).toHaveLength(3)
    expect(within(form).getByText(READ)).toBeTruthy()
    expect(within(form).getByText('hcp:coordination_context:write')).toBeTruthy()
    expect(within(form).queryByText(WORK)).toBeNull()
  })

  it('shows what each client may ask for, and edits it from the held scopes', async () => {
    render(<HcpClientsPage />)
    await screen.findByText(`May ask for ${READ}`)
    fireEvent.click(screen.getByRole('button', { name: 'Edit scopes' }))
    const group = await screen.findByRole('group', { name: 'Scopes for Recipe Planner' })
    expect(within(group).getAllByRole('checkbox')).toHaveLength(3) // exactly the held scopes
    expect(within(group).getByLabelText(new RegExp(`${WRITE}$`)) as HTMLInputElement).toHaveProperty('checked', false)
    expect(within(group).getByLabelText(new RegExp(`${READ}$`)) as HTMLInputElement).toHaveProperty('checked', true)

    fireEvent.click(within(group).getByLabelText(new RegExp(`${WRITE}$`)))
    fireEvent.click(within(group).getByRole('button', { name: 'Save scopes' }))
    await waitFor(() => expect(updateHcpClient).toHaveBeenCalledTimes(1))
    expect(updateHcpClient).toHaveBeenCalledWith('hcpc_1', {
      allowed_scopes: [READ, WRITE],
      rotate_webhook_secret: false,
    })
    await waitFor(() => expect(listHcpClients).toHaveBeenCalledTimes(2)) // reloaded
  })

  it('will not save an empty set', async () => {
    render(<HcpClientsPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit scopes' }))
    const group = await screen.findByRole('group', { name: 'Scopes for Recipe Planner' })
    fireEvent.click(within(group).getByLabelText(new RegExp(`${READ}$`)))
    expect((within(group).getByRole('button', { name: 'Save scopes' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('flags a registered scope the service no longer holds and blocks saving until it is removed', async () => {
    const gone = 'hcp:health_context:read'
    listHcpClients.mockResolvedValue([client({ allowed_scopes: [READ, gone] })])
    render(<HcpClientsPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit scopes' }))
    const group = await screen.findByRole('group', { name: 'Scopes for Recipe Planner' })
    expect(within(group).getByText('the service no longer holds this')).toBeTruthy()
    const save = within(group).getByRole('button', { name: 'Save scopes' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    fireEvent.click(within(group).getByLabelText(new RegExp(`${gone}`)))
    expect(save.disabled).toBe(false)
  })

  it('keeps the list on screen and says why when the service refuses a change', async () => {
    updateHcpClient.mockRejectedValue(new Error('hcp:work_context:read is not an HCP scope this instance holds'))
    render(<HcpClientsPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit scopes' }))
    const group = await screen.findByRole('group', { name: 'Scopes for Recipe Planner' })
    fireEvent.click(within(group).getByLabelText(new RegExp(`${WORK}$`)))
    fireEvent.click(within(group).getByRole('button', { name: 'Save scopes' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('is not an HCP scope this instance holds')
    expect(screen.getByText('Recipe Planner')).toBeTruthy()
  })

  it('registers with only the ticked scopes', async () => {
    registerHcpClient.mockResolvedValue(client({ name: 'New App' }))
    render(<HcpClientsPage />)
    const form = await screen.findByRole('form', { name: 'Register a client' })
    fireEvent.change(within(form).getByLabelText('Name'), { target: { value: 'New App' } })
    fireEvent.change(within(form).getByLabelText('Operator'), { target: { value: 'Co' } })
    fireEvent.change(within(form).getByLabelText('Redirect URIs'), { target: { value: 'https://n.example/cb' } })
    fireEvent.click(within(form).getByLabelText(new RegExp(`${WORK}$`)))
    fireEvent.click(within(form).getByRole('button', { name: 'Register' }))
    await waitFor(() => expect(registerHcpClient).toHaveBeenCalledTimes(1))
    expect(registerHcpClient.mock.calls[0][0].allowed_scopes).toEqual([WORK])
  })
})
