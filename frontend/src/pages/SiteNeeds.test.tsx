// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { SiteNeeds } from '@/api/connectedApps'

const { SiteNeedsTable } = await import('./ConnectedAppsPage')

afterEach(cleanup)

const NEEDS: SiteNeeds = {
  url: 'https://labs.test/canopy/site.json',
  available: true,
  reason: '',
  fetched_at: '2026-10-22T22:00:00Z',
  stale: false,
  pages: [{ key: 'labs:workflow:run:data', title: 'A workflow run, read-only', scopes: ['workflow:read', 'data:read'] }],
  requires: { handles_sensitive: true, runner_flag: 'sensitive_data' },
  uses_version: 'old',
  site_agents_version: 'new',
  agents: [
    { slug: 'labsdata', name: 'labsdata', offered: true, real_data: true, ready: true, meets_real_data: true, why_not: [] },
    {
      slug: 'ace', name: 'ACE', offered: true, real_data: true, ready: true, meets_real_data: false,
      why_not: ['does not handle sensitive data in canopy: no capability of ACE for this site says `data: sensitive`'],
    },
  ],
  not_allowed: ['ghost'],
}

describe('SiteNeedsTable', () => {
  it("shows the site's pages, what real data needs, and why an agent falls short", () => {
    render(<SiteNeedsTable needs={NEEDS} />)
    expect(screen.getByText('A workflow run, read-only')).toBeTruthy()
    expect(screen.getByText('workflow:read data:read')).toBeTruthy()
    expect(screen.getByText(/handles sensitive data and a ready sensitive-data runner/)).toBeTruthy()
    expect(screen.getByText(/does not handle sensitive data in canopy/)).toBeTruthy()
    expect(screen.getByText(/older site agents version/)).toBeTruthy()
    expect(screen.getByText(/names ghost/)).toBeTruthy()
  })

  it('says why when the document cannot be read, and shows no fit', () => {
    render(
      <SiteNeedsTable needs={{ ...NEEDS, available: false, reason: 'unreachable: 503', agents: [], pages: [] }} />,
    )
    expect(screen.getByText(/not available/)).toBeTruthy()
    expect(screen.getByText(/unreachable: 503/)).toBeTruthy()
    expect(screen.queryByText('labsdata')).toBeNull()
  })

  it('marks a stale copy', () => {
    render(<SiteNeedsTable needs={{ ...NEEDS, stale: true, reason: 'unreachable: timeout' }} />)
    expect(screen.getByText(/last copy canopy could verify/)).toBeTruthy()
  })
})
