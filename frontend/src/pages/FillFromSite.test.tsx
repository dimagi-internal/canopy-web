// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SiteDiscovery } from '@/api/connectedApps'

const discoverSite = vi.fn()
vi.mock('@/api/connectedApps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/connectedApps')>()),
  discoverSite: (...args: unknown[]) => discoverSite(...args),
}))

const { FillFromSite, SiteAddressFill, discoverySource } = await import('./ConnectedAppsPage')

afterEach(() => {
  cleanup()
  discoverSite.mockReset()
})

const SITE = {
  origins: ['https://labs.example.org'],
  jwks_url: 'https://labs.example.org/canopy/jwks.json',
  issuer: 'https://labs.example.org',
  mcp_resource: 'https://labs.example.org/mcp/',
}

const FOUND: SiteDiscovery = {
  url: 'https://labs.example.org/.well-known/canopy-site.json',
  tried: ['https://labs.example.org/.well-known/canopy-site.json'],
  name: 'connect-labs',
  site: SITE,
  signed_by: 'registered_keys',
  registered_keys_verify: true,
  current: { origins: ['http://localhost:8000'], jwks_url: '', issuer: '', mcp_resource: '' },
  changes: ['origins', 'jwks_url', 'issuer', 'mcp_resource'],
}

/**
 * A Connected site used to be a hand-typed copy of facts the site already
 * publishes. The button reads them from the site's signed site.json — and
 * saves NOTHING until the owner has seen them: a site must not be able to
 * change its own trusted origins by publishing a document.
 */
describe("Fill from the site's own documents", () => {
  it('reads THIS site, shows what it says beside what canopy has, and saves only on confirm', async () => {
    discoverSite.mockResolvedValue(FOUND)
    const onSave = vi.fn()
    render(<FillFromSite slug="connect" app={{ id: 7 }} busy={false} onSave={onSave} />)

    fireEvent.click(screen.getByRole('button', { name: "Fill from the site's own documents" }))

    await waitFor(() => expect(screen.getByText('https://labs.example.org/mcp/')).toBeTruthy())
    expect(discoverSite).toHaveBeenCalledWith('connect', { appId: 7 })
    expect(screen.getByText('http://localhost:8000')).toBeTruthy()
    expect(screen.getAllByText('changes')).toHaveLength(4)
    expect(onSave).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Save these values' }))
    expect(onSave).toHaveBeenCalledWith({
      origins: ['https://labs.example.org'],
      jwks_url: 'https://labs.example.org/canopy/jwks.json',
      host_issuer: 'https://labs.example.org',
      host_mcp_resource: 'https://labs.example.org/mcp/',
    })
  })

  it('offers nothing to save when canopy already has it', async () => {
    discoverSite.mockResolvedValue({ ...FOUND, current: SITE, changes: [] })
    render(<FillFromSite slug="connect" app={{ id: 7 }} busy={false} onSave={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: "Fill from the site's own documents" }))
    await waitFor(() => expect(screen.getByText('canopy already has what the site says.')).toBeTruthy())
    expect(screen.queryByRole('button', { name: 'Save these values' })).toBeNull()
  })

  it("shows canopy's refusal instead of a table", async () => {
    discoverSite.mockRejectedValue(new Error('no_facts: upgrade the site to dimagi-canopy 0.16'))
    const onSave = vi.fn()
    render(<FillFromSite slug="connect" app={{ id: 7 }} busy={false} onSave={onSave} />)
    fireEvent.click(screen.getByRole('button', { name: "Fill from the site's own documents" }))
    await waitFor(() => expect(screen.getByText(/no_facts/)).toBeTruthy())
    expect(screen.queryByRole('table')).toBeNull()
    expect(onSave).not.toHaveBeenCalled()
  })

  it('warns when the document is not signed with the keys canopy already holds', () => {
    expect(discoverySource({ ...FOUND, signed_by: 'published_keys', registered_keys_verify: false })).toMatch(
      /NOT signed with the keys canopy has/,
    )
  })
})

describe('connecting a site from its address', () => {
  it('reads the address and hands the values to the form', async () => {
    discoverSite.mockResolvedValue({ ...FOUND, current: null, changes: [], signed_by: 'published_keys',
      registered_keys_verify: null })
    const onFill = vi.fn()
    render(<SiteAddressFill slug="connect" onFill={onFill} />)

    fireEvent.change(screen.getByLabelText('Site address'), { target: { value: ' https://labs.example.org ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Fill from the site' }))

    await waitFor(() => expect(onFill).toHaveBeenCalled())
    expect(discoverSite).toHaveBeenCalledWith('connect', { address: 'https://labs.example.org' })
    expect(onFill.mock.calls[0][0].site).toEqual(SITE)
    expect(screen.getByText(/Check the values below, then Connect/)).toBeTruthy()
  })
})
