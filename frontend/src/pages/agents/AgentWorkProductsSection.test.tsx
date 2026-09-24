// @vitest-environment jsdom
//
// "Syncs" was a rail entry, and a manager sync is a periodic self-review of the
// agent's work with its own grades — which "sync" does not say (a data sync? a
// repo sync?). It is Status reports now, beside the work it reports on.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const { listAgentWorkProducts, listAgentSyncs } = vi.hoisted(() => ({
  listAgentWorkProducts: vi.fn<() => Promise<{ items: unknown[] }>>(),
  listAgentSyncs: vi.fn<() => Promise<{ items: unknown[] }>>(),
}))
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listAgentWorkProducts, listAgentSyncs,
}))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({ agent: { slug: 'echo', name: 'Echo', workspace: 'connect' } }),
}))

const { AgentWorkProductsSection } = await import('./AgentWorkProductsSection')

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('AgentWorkProductsSection', () => {
  it('shows deliverables and status reports on one page', async () => {
    listAgentWorkProducts.mockResolvedValue({ items: [
      { id: 1, title: 'The Q3 story', kind: 'doc', url: 'https://d/1', description: '', tags: [], source: '' },
    ] })
    listAgentSyncs.mockResolvedValue({ items: [
      { id: 9, title: 'September review', summary: 'went fine', doc_url: 'https://d/9',
        period_start: '2026-09-01T00:00:00Z', period_end: '2026-09-30T00:00:00Z',
        self_grades: { work: 'B' }, source: 'manager-sync', agent_slug: 'echo' },
    ] })
    render(
      <MemoryRouter initialEntries={['/w/connect/agents/echo/work-products']}>
        <Routes><Route path="/w/:workspace/agents/:slug/work-products" element={<AgentWorkProductsSection />} /></Routes>
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByRole('region', { name: 'Deliverables' })).toBeTruthy())
    const reports = screen.getByRole('region', { name: 'Status reports' })
    expect(within(reports).getByText(/September review/)).toBeTruthy()
    // The word that meant nothing is gone from the page.
    expect(screen.getByRole('region', { name: 'Status reports' }).textContent).not.toContain('Sync')
  })

  it('says so when there are no status reports, rather than hiding the section', async () => {
    listAgentWorkProducts.mockResolvedValue({ items: [] })
    listAgentSyncs.mockResolvedValue({ items: [] })
    render(
      <MemoryRouter initialEntries={['/w/connect/agents/echo/work-products']}>
        <Routes><Route path="/w/:workspace/agents/:slug/work-products" element={<AgentWorkProductsSection />} /></Routes>
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByText('No status reports yet.')).toBeTruthy())
  })
})
