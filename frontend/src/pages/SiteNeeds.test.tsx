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
    {
      slug: 'labsdata', name: 'labsdata', url: 'https://canopy.test/w/connect/agents/labsdata',
      offered: true, sensitive_data: true, ready: true, can_handle_sensitive_data: true, why_not: [],
    },
    {
      slug: 'ace', name: 'ACE', url: 'https://canopy.test/w/connect/agents/ace',
      offered: true, sensitive_data: false, ready: true, can_handle_sensitive_data: null, why_not: [],
    },
    {
      slug: 'scout', name: 'Scout', url: 'https://canopy.test/w/connect/agents/scout',
      offered: true, sensitive_data: true, ready: true, can_handle_sensitive_data: false,
      why_not: [{
        text: 'no capability of Scout for this site says `data: sensitive`',
        url: 'https://canopy.test/w/connect/agents/scout/settings#reach',
      }],
    },
  ],
  not_allowed: ['ghost'],
}

describe('SiteNeedsTable', () => {
  it('leads with the agents and says only what the owner can act on', () => {
    const { container } = render(<SiteNeedsTable needs={NEEDS} />)
    expect(screen.getByText('Can handle sensitive data')).toBeTruthy()
    expect(
      screen.getByText(
        /For sensitive data, this site requires an agent capability that handles sensitive data, running on a sensitive-data runner/,
      ),
    ).toBeTruthy()
    expect(screen.getByText(/older site agents version/)).toBeTruthy()
    expect(screen.getByText(/names ghost/)).toBeTruthy()
    // The agents table comes before the site's pages, which sit collapsed for reference.
    const details = container.querySelector('details')!
    expect(details.open).toBe(false)
    expect(details.textContent).toContain('A workflow run, read-only')
    expect(details.textContent).toContain('workflow:read data:read')
    expect(container.querySelector('table')!.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByText(/real data/i)).toBeNull()
  })

  it('says "not needed" for an agent the site never lets near its sensitive data, and marks nothing', () => {
    render(<SiteNeedsTable needs={NEEDS} />)
    const ace = screen.getByText('ACE').closest('tr')!
    expect(ace.textContent).toContain('not needed')
    expect(ace.getAttribute('data-attention')).toBeNull()
    expect(ace.querySelector('.text-destructive')).toBeNull()
  })

  it('marks only an offered agent allowed sensitive data that cannot handle it, with a link to fix it', () => {
    const { container } = render(<SiteNeedsTable needs={NEEDS} />)
    const scout = screen.getByText('Scout').closest('tr')!
    expect(scout.getAttribute('data-attention')).toBe('true')
    expect(scout.querySelector('.text-destructive')?.textContent).toContain('no capability of Scout')
    const fix = screen.getByRole('link', { name: 'fix in settings' })
    expect(fix.getAttribute('href')).toBe('https://canopy.test/w/connect/agents/scout/settings#reach')
    expect(container.querySelectorAll('[data-attention]')).toHaveLength(1)
    expect(screen.getByText('labsdata').closest('tr')!.textContent).toContain('yes')
  })

  it('does not mark an allowed agent that cannot handle it when the site does not offer it', () => {
    const agents = NEEDS.agents.map((a) => (a.slug === 'scout' ? { ...a, offered: false } : a))
    const { container } = render(<SiteNeedsTable needs={{ ...NEEDS, agents }} />)
    expect(container.querySelectorAll('[data-attention]')).toHaveLength(0)
    expect(container.querySelector('.text-destructive')).toBeNull()
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
