// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const getBrainState = vi.fn()
const getBrainLinks = vi.fn()
const getBrainRuns = vi.fn()
const listBrainOutputs = vi.fn()
const runLearnerNow = vi.fn()
vi.mock('@/api/brain', () => ({ getBrainState, getBrainLinks, getBrainRuns, listBrainOutputs, runLearnerNow }))

const { BrainPage } = await import('./BrainPage')

const learner = (slug: string, purpose_class: string) => ({
  slug, name: slug, purpose_class, enabled: true, scope: 'full', schedule: '', operator: 'ada', accountable: 'JJ',
})

const STATE = (owner: boolean) => ({
  reads_enabled: true,
  workspace: 'dimagi',
  can_run: owner,
  workspaces: ['dimagi'],
  admin_workspaces: owner ? ['dimagi'] : [],
  canopy_admin: false,
  consent: {
    mine: ['system'],
    workspaces: [
      { workspace: 'dimagi', name: 'Dimagi', members: 12, agreed_any: 3, agreed: { individual: 1, memory: 0, project: 3, system: 2 } },
    ],
  },
  sources: [{ workspace: 'dimagi', scope: 'yours', open: 4, withdrawn: 1, sealed: { sensitive: 2 } }],
  links: [{ project: 'supply', name: 'Supply', workspace: 'dimagi', tier: 'core', count: 2 }],
  learners: [learner('system-learning', 'system'), learner('project-learning', 'project'), learner('coaching', 'individual')],
  runs: { open: 0, done: 1, failed: 0 },
  cascade: [],
})

const RUN = {
  id: 3, learner: 'system-learning', purpose: 'improve', subject_type: 'system', subject: 'system', state: 'done',
  created_at: new Date().toISOString(), finished_at: null, reads: 2,
  sessions: [{ session_id: 's1', workspace: 'dimagi', title: 'runner debugging' }], hidden: 1,
}

function setup(owner = false) {
  getBrainState.mockResolvedValue(STATE(owner))
  getBrainLinks.mockResolvedValue({ links: [{ id: 9, project: 'supply', name: 'Supply', workspace: 'dimagi', tier: 'core',
    how: 'declared', confidence: null, evidence: [], session_id: 's1', title: 'tender work', started_at: '' }],
  next_before: 9, done: true })
  getBrainRuns.mockResolvedValue({ runs: [RUN], next_before: 3, done: true })
  listBrainOutputs.mockResolvedValue([{
    id: 1, learner: 'system-learning', kind: 'finding', workspace: 'dimagi', subject_type: 'system', subject: '',
    title: 'The runner idles without a delegation', body: { severity: 'high', fix: 'surface the 409', nested: { x: 1 } },
    state: 'active', created_at: new Date().toISOString(),
    sessions: [{ session_id: 's1', workspace: 'dimagi', title: 'runner debugging' }], hidden_sessions: 2,
  }])
  runLearnerNow.mockResolvedValue({ ...RUN, id: 4, state: 'open' })
  return render(
    <MemoryRouter initialEntries={['/w/dimagi/brain']}>
      <Routes>
        <Route path="/w/:workspace/brain" element={<BrainPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('BrainPage', () => {
  it('shows the live state: rollout, consent, sources, links to sessions, learners and runs', async () => {
    setup()
    expect(await screen.findByText('Reads on')).toBeTruthy()
    // Every read is pinned to the page's workspace.
    expect(getBrainState).toHaveBeenCalledWith('dimagi')
    await waitFor(() => expect(getBrainLinks).toHaveBeenCalledWith('dimagi', 0))
    expect(getBrainRuns).toHaveBeenCalledWith('dimagi', 0)
    expect(listBrainOutputs).toHaveBeenCalledWith('dimagi')
    expect(within(screen.getByRole('region', { name: 'Consent' })).getByText('12')).toBeTruthy()
    expect(within(screen.getByRole('region', { name: 'Sources' })).getByText('sensitive 2')).toBeTruthy()
    const links = screen.getByRole('region', { name: 'Stored links' })
    await waitFor(() => expect(within(links).getByRole('link', { name: 'tender work' }).getAttribute('href'))
      .toBe('/w/dimagi/sessions/s1'))
    const runs = screen.getByRole('region', { name: 'Runs' })
    expect(await within(runs).findByText(/\+1 you do not read/)).toBeTruthy()
    expect(screen.queryByRole('tab')).toBeNull()                          // one page, no preview tab
    expect(screen.queryByRole('button', { name: 'Run now' })).toBeNull()   // not this workspace's owner
  })

  it('renders findings with their fields and the sessions they cite', async () => {
    setup()
    const findings = await screen.findByRole('region', { name: 'Findings' })
    expect(await within(findings).findByText('The runner idles without a delegation')).toBeTruthy()
    expect(within(findings).getByText('severity')).toBeTruthy()
    expect(within(findings).getByText('surface the 409')).toBeTruthy()
    expect(within(findings).queryByText('nested')).toBeNull()
    expect(within(findings).getByRole('link', { name: 'runner debugging' }).getAttribute('href'))
      .toBe('/w/dimagi/sessions/s1')
    expect(findings.textContent).toContain('2 sessions you do not read')
  })

  it('lets the workspace owner run a learner now for this workspace, a project learner for a named project', async () => {
    setup(true)
    const learners = await screen.findByRole('region', { name: 'Learners' })
    const buttons = within(learners).getAllByRole('button', { name: 'Run now' })
    expect(buttons).toHaveLength(2)                                       // system and project; not the per-person one
    expect(within(learners).getByText('per person')).toBeTruthy()
    fireEvent.click(buttons[0])
    await waitFor(() => expect(runLearnerNow).toHaveBeenCalledWith('dimagi', 'system-learning', ''))
    expect(await within(learners).findByText('Run 4 started')).toBeTruthy()
    await waitFor(() => expect(getBrainRuns).toHaveBeenCalledTimes(2))    // the runs list reads again
    expect((buttons[1] as HTMLButtonElement).disabled).toBe(true)        // no project named yet
    fireEvent.change(within(learners).getByLabelText('Project for project-learning'), { target: { value: 'supply' } })
    fireEvent.click(buttons[1])
    await waitFor(() => expect(runLearnerNow).toHaveBeenCalledWith('dimagi', 'project-learning', 'supply'))
  })

  it('shows why a run cannot start, in the server\'s words', async () => {
    setup(true)
    runLearnerNow.mockRejectedValue(new Error("canopy's learning reads are switched off (BRAIN_READS_ENABLED)"))
    const learners = await screen.findByRole('region', { name: 'Learners' })
    fireEvent.click(within(learners).getAllByRole('button', { name: 'Run now' })[0])
    expect(await within(learners).findByText(/switched off/)).toBeTruthy()
  })
})
