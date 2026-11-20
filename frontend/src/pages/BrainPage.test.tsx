// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

const getBrainState = vi.fn()
const getBrainLinks = vi.fn()
const getBrainRuns = vi.fn()
const listBrainOutputs = vi.fn()
const getWouldRead = vi.fn()
const getPreviewTranscript = vi.fn()
const listBrainPreviews = vi.fn()
const createBrainPreview = vi.fn()
const deleteBrainPreview = vi.fn()
vi.mock('@/api/brain', () => ({
  LIVE: 'live',
  getBrainState,
  getBrainLinks,
  getBrainRuns,
  listBrainOutputs,
  getWouldRead,
  getPreviewTranscript,
  listBrainPreviews,
  createBrainPreview,
  deleteBrainPreview,
}))

const { BrainPage } = await import('./BrainPage')

const LIVE_SRC = { kind: 'live', id: null, window: null, created_at: null }
const PREVIEW_SRC = {
  kind: 'preview', id: 7, created_at: '2026-11-20T10:00:00Z',
  window: { since: '2026-11-19T10:00:00Z', until: '2026-11-20T10:00:00Z' },
}

const state = (source: object) => ({
  reads_enabled: false,
  workspaces: ['dimagi'],
  admin_workspaces: [],
  canopy_admin: false,
  consent: {
    mine: ['project'],
    workspaces: [
      { workspace: 'dimagi', name: 'Dimagi', members: 12, agreed_any: 3, agreed: { individual: 1, memory: 0, project: 3, system: 2 } },
    ],
  },
  sources: [{ workspace: 'dimagi', scope: 'yours', open: 4, withdrawn: 1, sealed: { sensitive: 2 } }],
  links: [{ project: 'supply', name: 'Supply', workspace: 'dimagi', tier: 'core', count: 2 }],
  learners: [
    { slug: 'project-learning', name: 'Project learning', purpose_class: 'project', enabled: false, scope: 'full',
      schedule: 'daily', operator: '', accountable: '' },
  ],
  runs: { open: 0, done: 1, failed: 0 },
  cascade: [],
  source,
})

const verdict = (ok: boolean, reason = '') => ({ ok, reason })
const ROW = {
  id: 's1',
  title: 'tender work',
  workspace: { slug: 'dimagi', name: 'Dimagi' },
  started_at: new Date().toISOString(),
  participants: [
    { user_id: 1, contact_id: null, name: 'Alice' },
    { user_id: 2, contact_id: null, name: 'Bob' },
  ],
  would_read: {
    project: { now: verdict(false, 'no_consent:2'), if_everyone_agreed: verdict(true) },
    system: { now: verdict(false, 'no_consent:1'), if_everyone_agreed: verdict(true) },
    individual: {},
  },
  you: { now: verdict(true), if_everyone_agreed: verdict(true) },
  links: [],
}
const wouldRead = (source: object) => ({
  window: { since: '2026-11-19T10:00:00Z', until: '2026-11-20T10:00:00Z' },
  total: 1,
  sessions: [ROW],
  counts: { sessions: 1, links: 1, sealed: {}, project: { now: 0, if_everyone_agreed: 1 },
            system: { now: 0, if_everyone_agreed: 1 }, individual: { now: 1, if_everyone_agreed: 1 } },
  would_link: [{ workspace: 'dimagi', project: 'supply', tier: 'core', sessions: [
    { id: 's1', title: 'tender work', how: 'dispatched', evidence: ['task:7'], outranked_by: null }] }],
  next_after: 's1',
  done: true,
  source,
})

function Where() {
  const l = useLocation()
  return <span data-testid="where">{l.pathname + l.search}</span>
}

function setup(path = '/brain') {
  const preview = path.includes('preview=')
  const src = preview ? PREVIEW_SRC : LIVE_SRC
  getBrainState.mockResolvedValue(state(src))
  getBrainLinks.mockResolvedValue({ links: [{ id: 9, project: 'supply', name: 'Supply', workspace: 'dimagi', tier: 'core',
    how: 'declared', confidence: null, evidence: [], session_id: 's1', title: 'tender work', started_at: '' }],
  next_before: 9, done: true, source: src })
  getBrainRuns.mockResolvedValue({ runs: [{ id: 3, learner: 'proj', purpose: 'learn', subject_type: 'project',
    subject: 'supply', state: 'done', created_at: new Date().toISOString(), finished_at: null, reads: 2,
    sessions: [{ session_id: 's1', workspace: 'dimagi', title: 'tender work' }], hidden: 1 }], next_before: 3, done: true,
  source: src })
  listBrainOutputs.mockResolvedValue(preview
    ? [{ id: 1, learner: 'system-learning', kind: 'finding', workspace: 'dimagi', subject_type: 'system', subject: '',
         title: 'Runner idles', body: {}, state: 'active', created_at: new Date().toISOString(), sessions: ['s1'],
         source: src }]
    : [])
  getWouldRead.mockResolvedValue(wouldRead(src))
  getPreviewTranscript.mockResolvedValue({ view: 'full', truncated: false, chars: 10,
    messages: [{ index: 0, role: 'user', text: 'scrubbed words', author: { name: 'Alice' } }] })
  listBrainPreviews.mockResolvedValue([{ id: 7, source: 'preview:7', window: PREVIEW_SRC.window, view: 'full',
    created_at: PREVIEW_SRC.created_at, expires_at: '2026-11-27T10:00:00Z', sessions: 4, outputs: 1, truncated: false }])
  createBrainPreview.mockResolvedValue({ id: 8 })
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="*" element={<><BrainPage /><Where /></>} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('BrainPage — live', () => {
  it('reads the live source and shows the state, links to sessions, learners and runs', async () => {
    setup()
    expect(await screen.findByText('Reads off')).toBeTruthy()
    expect(getBrainState).toHaveBeenCalledWith('live')
    expect(screen.queryByTestId('brain-preview-banner')).toBeNull()
    const consent = screen.getByRole('region', { name: 'Consent' })
    expect(within(consent).getByText('12')).toBeTruthy()
    const links = screen.getByRole('region', { name: 'Project links' })
    await waitFor(() => expect(within(links).getByRole('link', { name: 'tender work' }).getAttribute('href'))
      .toBe('/w/dimagi/sessions/s1'))
    expect(getBrainLinks).toHaveBeenCalledWith('live', 0)
    expect(within(screen.getByRole('region', { name: 'Runs' })).getByText(/\+1 you do not read/)).toBeTruthy()
    const outputs = screen.getByRole('region', { name: 'Outputs you may see' })
    expect(outputs.textContent).toContain('/canopy:brain-preview --publish <preview id>')
    expect(screen.getByRole('link', { name: 'Who works with whom' }).getAttribute('href')).toBe('/w/dimagi/network')
  })

  it('runs a preview and switches the page to it', async () => {
    setup()
    await screen.findByText('Reads off')
    fireEvent.change(screen.getByLabelText('Preview window'), { target: { value: '168' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run a preview' }))
    await waitFor(() => expect(createBrainPreview).toHaveBeenCalledWith(168))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/brain?preview=8'))
  })
})

describe('BrainPage — a preview is the same screens from another source', () => {
  it('passes the source through every read and shows one banner with the way out', async () => {
    setup('/brain?preview=7')
    const banner = await screen.findByTestId('brain-preview-banner')
    expect(banner.textContent).toContain('Preview: what the brain would do')
    expect(getBrainState).toHaveBeenCalledWith('preview:7')
    await waitFor(() => expect(getBrainLinks).toHaveBeenCalledWith('preview:7', 0))
    expect(getBrainRuns).toHaveBeenCalledWith('preview:7', 0)
    expect(listBrainOutputs).toHaveBeenCalledWith('preview:7')
    // Links keep the source; the recommendation shows as an output, marked preview.
    const links = screen.getByRole('region', { name: 'Project links' })
    await waitFor(() => expect(within(links).getByRole('link', { name: 'tender work' }).getAttribute('href'))
      .toBe('/w/dimagi/sessions/s1?preview=7'))
    const outputs = screen.getByRole('region', { name: 'Outputs you may see' })
    expect(await within(outputs).findByText('Runner idles')).toBeTruthy()
    expect(within(outputs).getByText('preview')).toBeTruthy()
    expect(outputs.textContent).toContain('/canopy:brain-preview --publish 7')
    expect((screen.getByLabelText('Source') as HTMLSelectElement).value).toBe('7')
    fireEvent.click(within(banner).getByRole('link', { name: 'Exit preview' }))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/brain'))
    await waitFor(() => expect(getBrainState).toHaveBeenLastCalledWith('live'))
  })

  it('shows the sessions tab from the preview, a transcript only on click', async () => {
    setup('/brain?preview=7&tab=sessions')
    const sessions = await screen.findByRole('region', { name: 'Sessions' })
    expect(getWouldRead).toHaveBeenCalledWith('preview:7', '')
    expect(within(sessions).getByText('no consent: Bob')).toBeTruthy()
    expect(within(screen.getByRole('region', { name: 'Links it would store' })).getByText('task:7')).toBeTruthy()
    expect(getPreviewTranscript).not.toHaveBeenCalled()
    fireEvent.click(within(sessions).getByRole('button', { name: 'Transcript' }))
    expect(await screen.findByText('scrubbed words')).toBeTruthy()
    expect(getPreviewTranscript).toHaveBeenCalledWith('2026-11-19T10:00:00Z', '2026-11-20T10:00:00Z', 's1')
  })
})
