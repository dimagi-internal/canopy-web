// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { listMyConnections } = vi.hoisted(() => ({ listMyConnections: vi.fn() }))
vi.mock('@/api/connections', async (orig) => ({
  ...(await orig<typeof import('@/api/connections')>()),
  listMyConnections,
}))

import { AllowAccessPage, customMinutes, GRANT_CHOICES, LONGEST_GRANT_MINUTES } from './AllowAccessPage'

const at = (url: string) => render(
  <MemoryRouter initialEntries={[url]}>
    <Routes><Route path="/w/:workspace/connections/:provider/allow" element={<AllowAccessPage />} /></Routes>
  </MemoryRouter>,
)

/** What the form would send, as the server reads it. */
function sent(): Record<string, string> {
  const form = screen.getByTestId('allow-access').querySelector('form') as HTMLFormElement
  return Object.fromEntries(Array.from(new FormData(form).entries()).filter(([k]) => k !== 'length')) as Record<string, string>
}

beforeEach(() => {
  vi.clearAllMocks()
  listMyConnections.mockResolvedValue({
    providers: [{ name: 'salesforce', title: 'Salesforce', workspace: 'w', workspace_name: 'W', configured: true }],
    connections: [], temporary: [],
  })
})
afterEach(cleanup)

describe('AllowAccessPage', () => {
  it('offers 1 hour, 8 hours, 1 day and 7 days, an hour chosen, and posts the start', async () => {
    at('/w/w/connections/salesforce/allow?return_to=%2Fw%2Fw%2Fapps%2Fsales%2Fmotions')
    expect(await screen.findByRole('heading', { name: 'Allow Salesforce for a while' })).toBeTruthy()
    expect(GRANT_CHOICES.map((c) => c.label)).toEqual(['1 hour', '8 hours', '1 day', '7 days'])
    const form = screen.getByTestId('allow-access').querySelector('form') as HTMLFormElement
    expect(form.method).toBe('post')
    expect(form.getAttribute('action')).toBe('/auth/connections/salesforce/w/start/')
    expect(sent()).toMatchObject({ temporary: '1', for_screens: '1', minutes: '60', return_to: '/w/w/apps/sales/motions' })
    expect(sent()).toHaveProperty('csrfmiddlewaretoken')
    fireEvent.click(screen.getByLabelText('7 days'))
    expect(sent().minutes).toBe(String(7 * 24 * 60))
    expect(screen.getByRole('button', { name: 'Continue to Salesforce' })).toBeTruthy()
  })

  it('takes another length in hours or days, up to 30 days', async () => {
    at('/w/w/connections/salesforce/allow')
    await screen.findByRole('heading', { name: 'Allow Salesforce for a while' })
    fireEvent.change(screen.getByLabelText('How many'), { target: { value: '3' } })
    expect(sent().minutes).toBe(String(3 * 24 * 60))
    fireEvent.change(screen.getByLabelText('Hours or days'), { target: { value: 'hours' } })
    expect(sent().minutes).toBe('180')
    fireEvent.change(screen.getByLabelText('Hours or days'), { target: { value: 'days' } })
    fireEvent.change(screen.getByLabelText('How many'), { target: { value: '31' } })
    expect(screen.getByRole('alert').textContent).toBe('The longest you can allow is 30 days.')
    expect(sent()).not.toHaveProperty('minutes')
    expect((screen.getByRole('button', { name: 'Continue to Salesforce' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('says plainly that canopy holds the sign-in until the end, and how to end it sooner', async () => {
    at('/w/w/connections/salesforce/allow')
    const page = await screen.findByTestId('allow-access')
    expect(page.textContent).toContain('canopy holds your Salesforce sign-in and can use it')
    expect(page.textContent).toContain('canopy ends it at Salesforce and deletes it')
    expect(page.textContent).toContain('Settings → Connections')
  })

  it('never sends the browser anywhere but this site afterwards', async () => {
    at('/w/w/connections/salesforce/allow?return_to=https%3A%2F%2Fevil.example%2F')
    await screen.findByTestId('allow-access')
    expect(sent().return_to).toBe('/settings')
  })
})

describe('customMinutes', () => {
  it('checks the limits the server checks', () => {
    expect(customMinutes('30', 'days')).toBe(LONGEST_GRANT_MINUTES)
    expect(customMinutes('0.05', 'hours')).toBe('Choose at least a few minutes.')
    expect(customMinutes('', 'hours')).toBe('Enter how many hours or days.')
  })
})
