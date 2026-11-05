// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const m = vi.hoisted(() => ({ getDynamicView: vi.fn(), getInstalledApp: vi.fn() }))
vi.mock('@/api/dynamicViews', async (orig) => ({ ...(await orig<typeof import('@/api/dynamicViews')>()), getDynamicView: m.getDynamicView }))
vi.mock('@/api/workspaceApps', async (orig) => ({ ...(await orig<typeof import('@/api/workspaceApps')>()), getInstalledApp: m.getInstalledApp }))
vi.mock('@/auth/AuthProvider', () => ({ useAuth: () => ({ user: { email: 'jj@dimagi.com' } }) }))
vi.mock('@/workspace/WorkspaceProvider', () => ({ useWorkspaceList: () => [], useWorkspaceRole: () => 'member' }))
vi.mock('@/lib/scopedLinks', () => ({ useScopedPath: () => (p: string) => p, scopedPath: (_: string, p: string) => p }))
vi.mock('@/dynamicViews/DynamicViewFrame', () => ({ DynamicViewFrame: () => <div data-testid="frame" /> }))
vi.mock('@/workspaceApps/AppFrame', () => ({ AppFrame: () => <div data-testid="frame" /> }))
vi.mock('@/workspaceApps/SetupList', () => ({ SetupList: () => null }))
vi.mock('./DynamicViewsPage', () => ({ useCanEditViews: () => false }))

import DynamicViewPage from './DynamicViewPage'
import WorkspaceAppPage from './WorkspaceAppPage'

const CONNECTIONS = [{ provider: 'salesforce', as: 'viewer', reads: [{ method: 'GET', path: 'query' }], writes: [{ method: 'PATCH', path: 'sobjects/Lead/*' }] }]

afterEach(cleanup)
beforeEach(() => vi.clearAllMocks())

describe('a dynamic view page', () => {
  it('says what the screen can do in Salesforce', async () => {
    m.getDynamicView.mockResolvedValue({ id: 1, title: 'Leads', code: '', sources: [], agent_tasks: [], connections: CONNECTIONS, version: 1, workspace: 'w' })
    render(<MemoryRouter initialEntries={['/w/views/1']}><Routes><Route path="/:workspace/views/:id" element={<DynamicViewPage />} /></Routes></MemoryRouter>)
    expect(await screen.findByText(/This screen can use your Salesforce:/)).toBeTruthy()
    expect(screen.getByText(/Change Leads/)).toBeTruthy()
  })
})

describe('an app page', () => {
  const app = (over: Record<string, unknown> = {}) => ({
    key: 'crm', name: 'CRM', enabled: true, my_roles: ['member'], may_maintain: false, connections: CONNECTIONS, data_model: {}, settings: {},
    pages: [{ slug: 'home', title: 'Home', audience: 'member', nav: true }, { slug: 'visit', title: 'Visit', audience: 'public', nav: true }], ...over,
  })
  const open = (url: string) => render(
    <MemoryRouter initialEntries={[url]}><Routes>
      <Route path="/:workspace/apps/:appKey" element={<WorkspaceAppPage />} />
    </Routes></MemoryRouter>,
  )

  it("says it on a member's page", async () => {
    m.getInstalledApp.mockResolvedValue(app())
    open('/w/apps/crm')
    expect(await screen.findByText(/This screen can use your Salesforce:/)).toBeTruthy()
  })

  it("does not say it on a draft's test copy, which never calls", async () => {
    m.getInstalledApp.mockResolvedValue(app({ draft: { number: 2 } }))
    open('/w/apps/crm?draft=2')
    await screen.findByTestId('frame')
    expect(screen.queryByText(/This screen can use/)).toBeNull()
  })
})
