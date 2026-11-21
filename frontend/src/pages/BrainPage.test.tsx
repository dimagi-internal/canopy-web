// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const getBrainState = vi.fn()
const getBrainLinks = vi.fn()
const getBrainRuns = vi.fn()
const listBrainOutputs = vi.fn()
const runTargetNow = vi.fn()
const addTarget = vi.fn()
const removeTarget = vi.fn()
const setLearningReads = vi.fn()
const configureLearner = vi.fn()
vi.mock('@/api/brain', () => ({
  getBrainState, getBrainLinks, getBrainRuns, listBrainOutputs, runTargetNow, addTarget, removeTarget, setLearningReads,
  configureLearner,
}))

const { BrainPage } = await import('./BrainPage')

const learner = (slug: string, purpose_class: string, more: Record<string, unknown> = {}) => ({
  slug, name: slug, purpose_class, enabled: true, scope: 'full', operator: 'ada', entry: '',
  accountable: 'JJ', runnable: purpose_class === 'system' || purpose_class === 'project', targets: 0,
  configurable: false, ...more,
})

const STATE = (owner: boolean) => ({
  learning_reads: {
    workspace: 'dimagi', on: true, own: true, source: 'dimagi', can_edit: owner,
    changes: owner ? [{ at: new Date().toISOString(), by: 'jj@dimagi.com', before: null, after: true, was_on: false,
      now_on: true }] : [],
  },
  workspace: 'dimagi',
  can_manage: owner,
  workspaces: ['dimagi'],
  admin_workspaces: owner ? ['dimagi'] : [],
  canopy_admin: false,
  consent: {
    mine: ['system'],
    workspaces: [
      { workspace: 'dimagi', name: 'Dimagi', members: 12, small: 3, agreed_any: 3, agreed: { individual: null, memory: null, project: 3, system: null } },
    ],
  },
  sources: [{ workspace: 'dimagi', scope: 'yours', open: 4, withdrawn: 1, sealed: { sensitive: 2 } }],
  links: [{ project: 'supply', name: 'Supply', workspace: 'dimagi', tier: 'core', count: 2 }],
  learners: [learner('system-learning', 'system', { configurable: owner }), learner('project-learning', 'project'),
    learner('coaching', 'individual'), learner('project-readers', 'project', { runnable: false, operator: '' })],
  targets: [
    { id: 7, learner: 'system-learning', workspace: 'dimagi', project: '', label: 'dimagi', schedule_id: 70,
      agent: 'ada', cron: '0 6 * * *', timezone: 'UTC', enabled: true, created_by: 'jj@dimagi.com', created_at: '',
      last_run: null, can_manage: owner },
    { id: 8, learner: 'project-learning', workspace: 'dimagi', project: 'supply', label: 'dimagi/supply', schedule_id: 80,
      agent: 'ada', cron: '0 7 * * 1', timezone: 'UTC', enabled: false, created_by: 'jj@dimagi.com', created_at: '',
      last_run: { id: 3, state: 'done', created_at: new Date().toISOString() }, can_manage: owner },
  ],
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
  runTargetNow.mockResolvedValue({ ...RUN, id: 4, state: 'open' })
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
    const reads = await screen.findByRole('region', { name: 'Learning reads' })
    expect(within(reads).getByText('On')).toBeTruthy()
    expect(within(reads).getByText('set here')).toBeTruthy()
    expect(within(reads).queryByRole('combobox')).toBeNull()              // a member sees it, never changes it
    // Every read is pinned to the page's workspace.
    expect(getBrainState).toHaveBeenCalledWith('dimagi')
    await waitFor(() => expect(getBrainLinks).toHaveBeenCalledWith('dimagi', 0))
    expect(getBrainRuns).toHaveBeenCalledWith('dimagi', 0)
    expect(listBrainOutputs).toHaveBeenCalledWith('dimagi')
    const consent = screen.getByRole('region', { name: 'Consent' })
    expect(within(consent).getByText('12')).toBeTruthy()
    expect(within(consent).getAllByText('<3')).toHaveLength(3)             // a count under three is never shown
    expect(within(screen.getByRole('region', { name: 'Sources' })).getByText('sensitive 2')).toBeTruthy()
    const links = screen.getByRole('region', { name: 'Stored links' })
    await waitFor(() => expect(within(links).getByRole('link', { name: 'tender work' }).getAttribute('href'))
      .toBe('/w/dimagi/sessions/s1'))
    const runs = screen.getByRole('region', { name: 'Runs' })
    expect(await within(runs).findByText(/\+1 you do not read/)).toBeTruthy()
    expect(screen.queryByRole('tab')).toBeNull()                          // one page, no preview tab
    expect(screen.queryByRole('button', { name: 'Run now' })).toBeNull()   // not this workspace's owner
    expect(screen.queryByRole('form', { name: 'Add a target' })).toBeNull()
    const aimed = screen.getByRole('region', { name: 'Targets' })
    expect(within(aimed).getByText('dimagi/supply')).toBeTruthy()            // everyone sees where learners run
    expect(within(aimed).getByText('(paused)', { exact: false })).toBeTruthy()
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

  it('lets its owners and admins run a target now, add one and take one away', async () => {
    setup(true)
    addTarget.mockResolvedValue({})
    removeTarget.mockResolvedValue(undefined)
    const aimed = await screen.findByRole('region', { name: 'Targets' })
    const buttons = within(aimed).getAllByRole('button', { name: 'Run now' })
    expect(buttons).toHaveLength(2)
    fireEvent.click(buttons[0])
    await waitFor(() => expect(runTargetNow).toHaveBeenCalledWith('dimagi', 7))
    expect(await within(aimed).findByText('Run 4 started')).toBeTruthy()
    await waitFor(() => expect(getBrainRuns).toHaveBeenCalledTimes(2))    // the runs list reads again
    const form = within(aimed).getByRole('form', { name: 'Add a target' })
    fireEvent.change(within(form).getByLabelText('Project'), { target: { value: 'supply' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Add target' }))
    await waitFor(() => expect(addTarget).toHaveBeenCalledWith('dimagi', {
      learner: 'system-learning', workspace: 'dimagi', project: 'supply', cron: '0 6 * * *', timezone: 'UTC',
    }))
    fireEvent.click(within(aimed).getAllByRole('button', { name: 'Remove' })[1])
    await waitFor(() => expect(removeTarget).toHaveBeenCalledWith('dimagi', 8))
  })

  it('shows the learners: built in and per person take no target', async () => {
    setup(true)
    const learners = await screen.findByRole('region', { name: 'Learners' })
    expect(within(learners).getByText('per person')).toBeTruthy()
    expect(within(learners).getByText('built in')).toBeTruthy()           // the project readers never run
    expect(within(learners).queryByRole('button', { name: 'Run now' })).toBeNull()
  })

  it('shows why a run cannot start, in the server\'s words', async () => {
    setup(true)
    runTargetNow.mockRejectedValue(new Error('learning reads are off in dimagi and the workspaces below it'))
    const aimed = await screen.findByRole('region', { name: 'Targets' })
    fireEvent.click(within(aimed).getAllByRole('button', { name: 'Run now' })[0])
    expect(await within(aimed).findByText(/learning reads are off in dimagi/)).toBeTruthy()
  })

  it('lets its owners and admins change learning reads, and lists every change', async () => {
    setup(true)
    setLearningReads.mockResolvedValue({})
    const reads = await screen.findByRole('region', { name: 'Learning reads' })
    expect(within(reads).getByText('jj@dimagi.com')).toBeTruthy()
    expect(within(reads).getByText('inherit → on')).toBeTruthy()
    const save = within(reads).getByRole('button', { name: 'Save' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)                                     // nothing changed yet
    fireEvent.change(within(reads).getByLabelText('Learning reads in dimagi'), { target: { value: 'inherit' } })
    fireEvent.click(save)
    await waitFor(() => expect(setLearningReads).toHaveBeenCalledWith('dimagi', null))
    await waitFor(() => expect(getBrainState).toHaveBeenCalledTimes(2))   // the page reads again
  })

  it('configures a learner its owners and admins may configure, and only that one', async () => {
    setup(true)
    configureLearner.mockResolvedValue({})
    const learners = await screen.findByRole('region', { name: 'Learners' })
    const configure = within(learners).getAllByRole('button', { name: 'Configure' })
    expect(configure).toHaveLength(1)                                     // only system-learning is configurable here
    fireEvent.click(configure[0])
    const form = within(learners).getByRole('form', { name: 'Configure system-learning' })
    fireEvent.change(within(form).getByLabelText('Entry command'), { target: { value: '/canopy:brain-learn' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(configureLearner).toHaveBeenCalledWith('dimagi', 'system-learning', {
      operator: 'ada', entry: '/canopy:brain-learn', enabled: true,
    }))
  })
})
