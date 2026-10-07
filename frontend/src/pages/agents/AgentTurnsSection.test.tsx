// @vitest-environment jsdom
//
// A manager sync is a periodic self-review of the agent's work with its own
// grades. "Sync" does not say that (a data sync? a repo sync?), so it is
// "Status reports" — a section on the Turns page, at `#status-reports`.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const { listAgentTurns, listAgentSyncs } = vi.hoisted(() => ({
  listAgentTurns: vi.fn<() => Promise<{ items: unknown[] }>>(),
  listAgentSyncs: vi.fn<() => Promise<{ items: unknown[] }>>(),
}))
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listAgentTurns, listAgentSyncs,
}))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({ agent: { slug: 'echo', name: 'Echo', workspace: 'connect' } }),
}))

const { AgentTurnsSection } = await import('./AgentTurnsSection')

afterEach(() => { cleanup(); vi.clearAllMocks() })

function renderTurns(hash = '') {
  render(
    <MemoryRouter initialEntries={[`/w/connect/agents/echo/turns${hash}`]}>
      <Routes><Route path="/w/:workspace/agents/:slug/turns" element={<AgentTurnsSection />} /></Routes>
    </MemoryRouter>,
  )
}

describe('AgentTurnsSection status reports', () => {
  it('shows status reports in a #status-reports section', async () => {
    listAgentTurns.mockResolvedValue({ items: [] })
    listAgentSyncs.mockResolvedValue({ items: [
      { id: 9, title: 'September review', summary: 'went fine', doc_url: 'https://d/9',
        period_start: '2026-09-01T00:00:00Z', period_end: '2026-09-30T00:00:00Z',
        self_grades: { work: 'B' }, source: 'manager-sync', agent_slug: 'echo' },
    ] })
    renderTurns()
    const reports = await screen.findByRole('region', { name: 'Status reports' })
    expect(reports.id).toBe('status-reports')
    await waitFor(() => expect(within(reports).getByText(/September review/)).toBeTruthy())
    expect(reports.textContent).not.toContain('Sync')
  })

  it('says so when there are no status reports, rather than hiding the section', async () => {
    listAgentTurns.mockResolvedValue({ items: [] })
    listAgentSyncs.mockResolvedValue({ items: [] })
    renderTurns()
    await waitFor(() => expect(screen.getByText('No status reports yet.')).toBeTruthy())
  })

  it('scrolls to the element named by the hash on mount (a turn id, and #status-reports)', async () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    listAgentTurns.mockResolvedValue({ items: [{
      id: 'abc-123', agent_slug: 'echo', title: 'A turn', summary: '', status: 'completed',
      task_ext_ids: [], work_product_urls: [], session_slug: '', share_token: '',
    }] })
    listAgentSyncs.mockResolvedValue({ items: [] })
    renderTurns('#abc-123')
    await waitFor(() => expect(document.getElementById('abc-123')).toBeTruthy())
    await waitFor(() => expect(scroll).toHaveBeenCalled())
    expect(scroll.mock.instances.some((el) => (el as Element).id === 'abc-123')).toBe(true)
    cleanup()
    scroll.mockClear()
    renderTurns('#status-reports')
    await waitFor(() => expect(scroll.mock.instances.some((el) => (el as Element).id === 'status-reports')).toBe(true))
  })
})
