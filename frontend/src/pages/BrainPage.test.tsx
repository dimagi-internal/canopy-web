// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const getBrainState = vi.fn()
const getBrainLinks = vi.fn()
const getBrainRuns = vi.fn()
const listBrainOutputs = vi.fn()
const getPreviewSummary = vi.fn()
const getPreviewTranscript = vi.fn()
vi.mock('@/api/brain', () => ({
  getBrainState,
  getBrainLinks,
  getBrainRuns,
  listBrainOutputs,
  getPreviewSummary,
  getPreviewTranscript,
}))

const { BrainPage } = await import('./BrainPage')

const STATE = {
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
  cascade: [{ workspace: 'dimagi', scope: 'yours', rederive_open: 0, revocations_undelivered: 0, removed_recently: 1,
              outputs_withdrawn_recently: 0, outputs_stale_recently: 0 }],
}

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
const SUMMARY = {
  window: { since: '', until: '' },
  total: 1,
  sessions: [ROW],
  counts: { sessions: 1, links: 1, sealed: {}, project: { now: 0, if_everyone_agreed: 1 },
            system: { now: 0, if_everyone_agreed: 1 }, individual: { now: 1, if_everyone_agreed: 1 } },
  would_link: [{ workspace: 'dimagi', project: 'supply', tier: 'core', sessions: [
    { id: 's1', title: 'tender work', how: 'dispatched', evidence: ['task:7'], outranked_by: null }] }],
  next_after: 's1',
  done: true,
}

function setup(path = '/brain') {
  getBrainState.mockResolvedValue(STATE)
  getBrainLinks.mockResolvedValue({ links: [{ id: 9, project: 'supply', name: 'Supply', workspace: 'dimagi', tier: 'core',
    how: 'declared', confidence: null, evidence: [], session_id: 's1', title: 'tender work', started_at: '' }],
  next_before: 9, done: true })
  getBrainRuns.mockResolvedValue({ runs: [{ id: 3, learner: 'proj', purpose: 'learn', subject_type: 'project',
    subject: 'supply', state: 'done', created_at: new Date().toISOString(), finished_at: null, reads: 2,
    sessions: [{ session_id: 's1', workspace: 'dimagi', title: 'tender work' }], hidden: 1 }], next_before: 3, done: true })
  listBrainOutputs.mockResolvedValue([])
  getPreviewSummary.mockResolvedValue(SUMMARY)
  getPreviewTranscript.mockResolvedValue({ view: 'full', truncated: false, chars: 10,
    messages: [{ index: 0, role: 'user', text: 'scrubbed words', author: { name: 'Alice' } }] })
  return render(
    <MemoryRouter initialEntries={[path]}>
      <BrainPage />
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('BrainPage — Now', () => {
  it('shows the rollout, consent counts, sources, links to sessions, learners, runs and removals', async () => {
    setup()
    expect(await screen.findByText('Reads off')).toBeTruthy()
    const consent = screen.getByRole('region', { name: 'Consent' })
    expect(within(consent).getByText('Dimagi')).toBeTruthy()
    expect(within(consent).getByText('12')).toBeTruthy()
    expect(within(consent).getByText(/You agreed to/).textContent).toContain('project')
    expect(within(screen.getByRole('region', { name: 'Sources' })).getByText('sensitive 2')).toBeTruthy()
    const links = screen.getByRole('region', { name: 'Stored links' })
    await waitFor(() => expect(within(links).getByRole('link', { name: 'tender work' }).getAttribute('href'))
      .toBe('/w/dimagi/sessions/s1'))
    expect(within(screen.getByRole('region', { name: 'Learners' })).getByText('nobody yet')).toBeTruthy()
    const runs = screen.getByRole('region', { name: 'Runs' })
    expect(await within(runs).findByText(/\+1 you do not read/)).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Who works with whom' }).getAttribute('href')).toBe('/w/dimagi/network')
    // The preview is not asked for until its tab is open.
    expect(getPreviewSummary).not.toHaveBeenCalled()
  })
})

describe('BrainPage — Preview', () => {
  it('shows the server summary, the links it would store and a verdict table, with no transcript fetched', async () => {
    setup('/brain?tab=preview')
    const summary = await screen.findByRole('region', { name: 'Summary' })
    expect(within(summary).getByText('About you')).toBeTruthy()
    expect(getPreviewSummary).toHaveBeenCalledTimes(1)
    const [since, until] = getPreviewSummary.mock.calls[0]
    expect(Date.parse(until) - Date.parse(since)).toBe(24 * 3600_000)            // the last 24h by default
    const would = screen.getByRole('region', { name: 'Links it would store' })
    expect(within(would).getByText('task:7')).toBeTruthy()
    const sessions = screen.getByRole('region', { name: 'Sessions' })
    expect(within(sessions).getByText('no consent: Bob')).toBeTruthy()           // the reason, with the person's name
    expect(getPreviewTranscript).not.toHaveBeenCalled()
    fireEvent.click(within(sessions).getByRole('button', { name: 'Transcript' }))
    expect(await screen.findByText('scrubbed words')).toBeTruthy()
    expect(getPreviewTranscript).toHaveBeenCalledWith(since, until, 's1')
  })

  it('asks again for a wider window', async () => {
    setup('/brain?tab=preview')
    await screen.findByRole('region', { name: 'Summary' })
    fireEvent.change(screen.getByLabelText('Window'), { target: { value: '168' } })
    await waitFor(() => expect(getPreviewSummary).toHaveBeenCalledTimes(2))
    const [since, until] = getPreviewSummary.mock.calls[1]
    expect(Date.parse(until) - Date.parse(since)).toBe(168 * 3600_000)
  })

  it('is honest that recommendations are made on your own machine', async () => {
    setup('/brain?tab=preview')
    const rec = await screen.findByRole('region', { name: 'Recommendations' })
    expect(rec.textContent).toContain('/canopy:brain-preview')
    expect(rec.textContent).toContain('not on canopy-web yet')
  })
})
