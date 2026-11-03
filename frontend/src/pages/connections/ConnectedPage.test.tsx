// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getShareState = vi.fn()
vi.mock('@/api/connections', async (orig) => ({
  ...(await orig<typeof import('@/api/connections')>()),
  getShareState: (id: string) => getShareState(id),
}))

const unlock = vi.fn()
vi.mock('@/vault/useVaultUnlock', () => ({ useVaultUnlock: () => ({ unlock, element: null }) }))

import { ConnectedPage } from './ConnectedPage'
import { ApiError } from '@/api/problem'

const SERVER = {
  available: true, thumbprint: 'T', public_jwk: {}, on: true, statements_enabled: true, provider_calls: true, reachable: true,
}
const STATE = {
  id: 'c1', provider: 'salesforce', account_label: 'jj@dimagi.com', state: 'unshared', owner_agent: null, server: SERVER,
}

function open(query = '?return_to=%2Fsettings%2Fconnections') {
  return render(
    <MemoryRouter initialEntries={[`/w/dimagi/connections/c1/connected${query}`]}>
      <Routes>
        <Route path="/w/:workspace/connections/:id/connected" element={<ConnectedPage />} />
      </Routes>
    </MemoryRouter>,
  )
}
const href = (name: string | RegExp) => screen.getByRole('link', { name }).getAttribute('href')

beforeEach(() => {
  getShareState.mockResolvedValue(STATE)
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ConnectedPage', () => {
  it('says a person can use it now, with the two links', async () => {
    open()
    expect(await screen.findByText('Connected as jj@dimagi.com. Project screens and apps you use can use it now.')).toBeTruthy()
    expect(href('Also let agents or my computers use it')).toBe(
      '/w/dimagi/connections/c1/share?return_to=%2Fsettings%2Fconnections',
    )
    expect(href('Done')).toBe('/settings/connections')
  })

  it("says screens can use an agent's connection, with the one step left", async () => {
    getShareState.mockResolvedValue({ ...STATE, owner_agent: { id: '12', slug: 'eva', name: 'Eva' } })
    open()
    expect(await screen.findByText("Connected Eva as jj@dimagi.com. Screens can use it now. One step left for Eva's own runs:")).toBeTruthy()
    expect(href("Turn on for Eva's machines")).toBe('/w/dimagi/connections/c1/share?return_to=%2Fsettings%2Fconnections')
  })

  it('off: asks who can use it', async () => {
    getShareState.mockResolvedValue({ ...STATE, server: { ...SERVER, on: false, reachable: false } })
    open()
    expect(await screen.findByText('Connected as jj@dimagi.com. One step left: choose who can use it.')).toBeTruthy()
    expect(href('Choose')).toBe('/w/dimagi/connections/c1/share?return_to=%2Fsettings%2Fconnections')
  })

  it('a provider canopy-web never calls gets the off copy, even if the row says on', async () => {
    getShareState.mockResolvedValue({ ...STATE, provider: 'google', server: { ...SERVER, provider_calls: false } })
    open()
    expect(await screen.findByText('Connected as jj@dimagi.com. One step left: choose who can use it.')).toBeTruthy()
    expect(screen.queryByText(/Project screens and apps you use/)).toBeNull()
  })

  it("on but not reachable: says canopy-web couldn't be reached, never that screens can use it", async () => {
    getShareState.mockResolvedValue({ ...STATE, server: { ...SERVER, on: true, reachable: false } })
    open()
    expect(await screen.findByText(
      "Connected as jj@dimagi.com. canopy-web couldn't be reached to turn on project screens and apps. One step left: choose who can use it.",
    )).toBeTruthy()
    expect(screen.queryByText(/can use it now/)).toBeNull()
    expect(href('Choose')).toContain('/connections/c1/share')
  })

  it("the key can't be read right now: says so, with a person's links, and no choose step", async () => {
    getShareState.mockResolvedValue({ ...STATE, server: { ...SERVER, on: true, reachable: null } })
    open()
    expect(await screen.findByText(
      "Connected as jj@dimagi.com. canopy-web can't be reached right now, so project screens and apps may not work for a moment.",
    )).toBeTruthy()
    expect(href('Also let agents or my computers use it')).toContain('/connections/c1/share')
    expect(href('Done')).toBe('/settings/connections')
    expect(screen.queryByRole('link', { name: 'Choose' })).toBeNull()
  })

  it("the key can't be read right now: an agent's has its links too", async () => {
    getShareState.mockResolvedValue({
      ...STATE, owner_agent: { id: '12', slug: 'eva', name: 'Eva' }, server: { ...SERVER, on: true, reachable: null },
    })
    open()
    expect(await screen.findByText(
      "Connected Eva as jj@dimagi.com. canopy-web can't be reached right now, so project screens and apps may not work for a moment.",
    )).toBeTruthy()
    expect(href("Turn on for Eva's machines")).toContain('/connections/c1/share')
    expect(href('Done')).toBe('/settings/connections')
  })

  it('switched off with the key unreadable is the ordinary off copy', async () => {
    getShareState.mockResolvedValue({ ...STATE, server: { ...SERVER, on: false, reachable: false } })
    open()
    expect(await screen.findByText('Connected as jj@dimagi.com. One step left: choose who can use it.')).toBeTruthy()
  })

  it("an agent's Connected screen has a way back", async () => {
    getShareState.mockResolvedValue({ ...STATE, owner_agent: { id: '12', slug: 'eva', name: 'Eva' } })
    open()
    await screen.findByText(/Connected Eva as/)
    expect(href('Done')).toBe('/settings/connections')
  })

  it("an agent's choose step has a way back too", async () => {
    getShareState.mockResolvedValue({
      ...STATE, provider: 'google', owner_agent: { id: '12', slug: 'eva', name: 'Eva' },
      server: { ...SERVER, on: false, reachable: false, provider_calls: false },
    })
    open()
    await screen.findByText(/One step left/)
    expect(href('Done')).toBe('/settings/connections')
  })

  it('an empty account name falls back to the account id, never "Connected as ."', async () => {
    getShareState.mockResolvedValue({ ...STATE, account_label: '', account_id: '00Dxx/005yy' })
    open()
    expect(await screen.findByText('Connected as 00Dxx/005yy. Project screens and apps you use can use it now.')).toBeTruthy()
  })

  it('no vault prompt on this screen', async () => {
    open()
    await screen.findByText(/Connected as/)
    expect(unlock).not.toHaveBeenCalled()
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByText(/passkey/i)).toBeNull()
  })

  it('an unsafe return_to falls back to Settings for Done', async () => {
    open('?return_to=%2F%2Fevil.test')
    await screen.findByText(/Connected as/)
    expect(href('Done')).toBe('/settings')
  })

  it("when the connection can't be read here, still says it is connected and links to Share", async () => {
    getShareState.mockRejectedValue(new ApiError(403, 'x', { status: 403, code: 'not_the_owner_key', error: 'not_the_owner_key' }))
    open()
    expect(await screen.findByText('Connected. One step left: choose who can use it.')).toBeTruthy()
    expect(href('Choose')).toContain('/connections/c1/share')
  })
})
