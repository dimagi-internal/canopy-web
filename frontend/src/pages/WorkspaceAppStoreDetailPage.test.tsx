// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const detail = vi.fn()
const install = vi.fn()
vi.mock('@/api/workspaceApps', () => ({
  getAppStoreDetail: (w: string, t: string) => detail(w, t),
  installApp: (w: string, k: string) => install(w, k),
  appStoreScreenshotUrl: (_w: string, _k: string, f: string) => `/shots/${f}`,
}))
const role = vi.fn(() => 'admin')
vi.mock('@/workspace/WorkspaceProvider', () => ({ useWorkspaceList: () => [{ slug: 'dimagi', role: role() }] }))

import WorkspaceAppStoreDetailPage from './WorkspaceAppStoreDetailPage'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const HIRING = {
  key: 'hiring', name: 'Hiring', publisher: 'canopy', version: '2.2.0', description: 'An applicant tracker.', installed: false, settings: 12,
  listing: {
    tagline: 'From applied to hired.', overview: 'First paragraph.\n\nSecond paragraph.', audience: 'Hiring managers.', data: 'Applicants and offers.',
    highlights: [{ title: 'A pipeline', body: 'Drag people along.' }],
    screenshots: [{ file: 'a.png', caption: 'The pipeline' }, { file: 'b.png', caption: 'Reports' }],
  },
  roles: [{ name: 'recruiter', holders: 'editors', description: 'Runs the pipeline.' }],
  records: [{ label: 'Offers', scoped: true, restricted: ['recruiter'] }],
  retention: ['Rejected applicants after a year'],
  pages: [{ title: 'Pipeline', audience: 'member' }, { title: 'Careers', audience: 'public' }],
  public_pages: ['Careers'],
  emails: [{ name: 'Application received', to: 'to the applicant', optional: true, delayable: false }],
  versions: [{ version: '2.2.0', released_at: '2026-10-01T00:00:00Z', changelog: 'New reports.\n\nMore detail.' }],
}

function open() {
  return render(
    <MemoryRouter initialEntries={['/w/dimagi/apps/store?template=hiring']}>
      <Routes>
        <Route path="/w/:workspace/apps/store" element={<WorkspaceAppStoreDetailPage />} />
        <Route path="/w/:workspace/apps/:appKey" element={<div>installed app</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe("Hiring's store page, in the shared layout", () => {
  it('still shows everything someone deciding reads', async () => {
    detail.mockResolvedValue(HIRING)
    open()
    const page = await screen.findByTestId('app-detail')
    expect(detail).toHaveBeenCalledWith('dimagi', 'hiring')
    expect(within(page).getByRole('heading', { level: 1, name: 'Hiring' })).toBeTruthy()
    for (const text of ['by canopy · version 2.2.0', 'From applied to hired.', 'First paragraph.', 'Second paragraph.', 'A pipeline', 'Hiring managers.',
      'Applicants and offers.', 'Rejected applicants after a year', 'Pipeline', 'Application received', 'New reports.']) {
      expect(page.textContent).toContain(text)
    }
    expect(page.textContent).toContain('some fields recruiter only')
    expect(page.textContent).toContain('12 settings')
    expect(page.textContent).not.toContain('Unreleased')
  })

  it('moves through its screenshots', async () => {
    detail.mockResolvedValue(HIRING)
    open()
    expect((await screen.findByAltText('The pipeline')).getAttribute('src')).toBe('/shots/a.png')
    fireEvent.click(screen.getByRole('button', { name: 'Screenshot 2: Reports' }))
    expect(screen.getByAltText('Reports')).toBeTruthy()
  })

  it('installs for an admin and opens the app', async () => {
    detail.mockResolvedValue(HIRING)
    install.mockResolvedValue({ key: 'hiring' })
    open()
    fireEvent.click(await screen.findByRole('button', { name: 'Install' }))
    await waitFor(() => expect(install).toHaveBeenCalledWith('dimagi', 'hiring'))
    expect(await screen.findByText('installed app')).toBeTruthy()
  })

  it('tells anyone else an admin can install it', async () => {
    role.mockReturnValue('viewer')
    detail.mockResolvedValue(HIRING)
    open()
    expect(await screen.findByText('A workspace admin can install it.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Install' })).toBeNull()
    role.mockReturnValue('admin')
  })

  it('shows why when the app cannot be read', async () => {
    detail.mockRejectedValue(new Error('no such app'))
    open()
    expect(await screen.findByText('no such app')).toBeTruthy()
    expect(screen.getByRole('link', { name: '← Apps' }).getAttribute('href')).toBe('/w/dimagi/apps')
  })
})
