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
const acknowledgeFlag = vi.fn()
const setOutputStatus = vi.fn()
const setTargetBudget = vi.fn()
const whyNotRead = vi.fn()
const getBrainScorecard = vi.fn()
const getFixPlan = vi.fn()
const askFix = vi.fn()
vi.mock('@/api/brain', () => ({
  getBrainScorecard, getFixPlan, askFix,
  getBrainState, getBrainLinks, getBrainRuns, listBrainOutputs, runTargetNow, addTarget, removeTarget, setLearningReads,
  configureLearner, acknowledgeFlag, setOutputStatus, setTargetBudget, whyNotRead,
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
      last_run: null, can_manage: owner,
      budget: { max_sources_per_run: 50, max_outputs_per_run: 20, max_runs_per_day: 4, max_outputs_per_day: 50,
        max_tokens_per_day: null, today: { runs: 1, outputs: 2, tokens: 1200 } } },
    { id: 8, learner: 'project-learning', workspace: 'dimagi', project: 'supply', label: 'dimagi/supply', schedule_id: 80,
      agent: 'ada', cron: '0 7 * * 1', timezone: 'UTC', enabled: false, created_by: 'jj@dimagi.com', created_at: '',
      last_run: { id: 3, state: 'done', created_at: new Date().toISOString() }, can_manage: owner },
  ],
  flags: owner ? [{ id: 5, kind: 'github.pull', ref: 'https://github.com/dimagi-internal/canopy/pull/77', workspace: 'dimagi',
    run: 3, learner: 'system-learning', owner: 'jj@dimagi.com', reason: 'a session it came from was removed (excluded)',
    changed_at: new Date().toISOString(), acknowledged: false }] : [],
  runs: { open: 0, done: 1, failed: 0 },
  cascade: [],
})

const RUN = {
  id: 3, learner: 'system-learning', purpose: 'improve', subject_type: 'system', subject: 'system', state: 'done',
  created_at: new Date().toISOString(), finished_at: null, reads: 2,
  sessions: [{ session_id: 's1', workspace: 'dimagi', title: 'runner debugging' }], hidden: 1,
  context: { version: 1, run: 3, state: 'done', learner: 'system-learning', learner_name: 'System learning',
    purpose_class: 'system', purpose: 'improve', entry: '/canopy:brain-learn', output_kinds: ['finding'],
    subject_type: 'system', subject: 'dimagi', scope: 'dimagi',
    target: { id: 7, kind: 'workspace', workspace: 'dimagi', project: '', schedule_id: 70 },
    accountable: { email: 'jj@dimagi.com', name: 'JJ' }, sessions_due: 9, source_ids_due: [],
    budget: { max_sources_per_run: 50, max_outputs_per_run: 20, sources_left: 0, outputs_left: 0, stopped: '' },
    how: 'Read only with brain_read' },
}

const ago = (days: number) => new Date(Date.now() - days * 86_400_000).toISOString()
const finding = (id: number, more: Record<string, unknown> = {}) => ({
  id, learner: 'system-learning', kind: 'finding', workspace: 'dimagi', subject_type: 'system', subject: '', key: `k${id}`,
  title: `finding ${id}`, body: {}, state: 'active', status: 'open', seen: 1, may_set_status: true,
  created_at: ago(5), last_seen_at: ago(5), sessions: [], hidden_sessions: 0, source_sessions: [], ...more,
})
const WEEKS = Array.from({ length: 8 }, (_, i) => `2026-0${(i % 9) + 1}-0${(i % 9) + 1}`)
const SCORECARD = {
  weeks: WEEKS,
  targets: [{ target: 7, learner: 'system-learning', label: 'dimagi', workspace: 'dimagi', project: '',
    weeks: WEEKS.map((week, i) => ({ week, open: i === 7 ? 2 : 0, accepted: i === 6 ? 1 : 0, done: i === 5 ? 1 : 0,
      dismissed: i === 4 ? 2 : 0 })),
    open: 2, accepted: 1, done: 1, dismissed: 2, precision: 0.5, open_now: 2, faded_now: 1, came_back: 1 }],
}
const OUTPUTS = [
  finding(1, {
    title: 'The runner idles without a delegation', seen: 3, last_seen_at: ago(1),
    body: {
      severity: 'high', type: 'provision', signal: 'over_claim', suggested_fix: 'surface the 409',
      fix_belongs: { repo: 'canopy', agent: 'ada', skill: 'turn' },
      evidence: [
        { source: 41, index: 12, quote: 'claim refused: no delegation' },
        { source: 42, index: 3, quote: 'still waiting on the runner' },
        { source: 41, index: 30, quote: 'third quote' },
        { source: 41, index: 31, quote: 'fourth quote' },
      ],
      proof: 'proven', not_checked: 'whether it is fixed on main', nested: { x: 1 }, extra_note: ['a', 'b'],
      merged_from: [{ id: 8, key: 'canopy:x', seen: 1, title: 'An earlier duplicate' }],
    },
    sessions: [{ session_id: 's1', workspace: 'dimagi', title: 'runner debugging' }], hidden_sessions: 2,
    source_sessions: [{ source: 41, session_id: 's1', workspace: 'dimagi', title: 'runner debugging' }],
  }),
  finding(2, { title: 'A medium one seen lately', last_seen_at: ago(0), body: { severity: 'medium',
    fix_belongs: { repo: 'canopy-web' } } }),
  finding(3, { title: 'A high one seen long ago', last_seen_at: ago(9), body: { severity: 'high',
    fix_belongs: { repo: 'canopy-web' } } }),
  finding(4, { title: 'An old dismissed one', status: 'dismissed', body: { severity: 'high', fix_belongs: { repo: 'ace' } } }),
]

/** A finding's row, opened. */
async function openFinding(title: string): Promise<HTMLElement> {
  const card = await screen.findByRole('listitem', { name: title })
  fireEvent.click(within(card).getByRole('button', { name: new RegExp(title) }))
  return card
}

function setup(owner = false, url = '/w/dimagi/brain?tab=setup') {
  getBrainState.mockResolvedValue(STATE(owner))
  getBrainLinks.mockResolvedValue({ links: [{ id: 9, project: 'supply', name: 'Supply', workspace: 'dimagi', tier: 'core',
    how: 'declared', confidence: null, evidence: [], session_id: 's1', title: 'tender work', started_at: '' }],
  next_before: 9, done: true })
  getBrainRuns.mockResolvedValue({ runs: [RUN], next_before: 3, done: true })
  listBrainOutputs.mockResolvedValue(OUTPUTS)
  getBrainScorecard.mockResolvedValue(SCORECARD)
  runTargetNow.mockResolvedValue({ ...RUN, id: 4, state: 'open' })
  return render(
    <MemoryRouter initialEntries={[url]}>
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
  it('shows what a run was told, its target\'s budget and today\'s use', async () => {
    setup(true)
    expect(await screen.findByText('context')).toBeTruthy()
    expect(screen.getByText('jj@dimagi.com', { selector: 'dd' })).toBeTruthy()
    expect(screen.getByText(/today 1\/4 runs, 2\/50 outputs, 1200 tokens/)).toBeTruthy()
  })

  it('changes a target\'s budget', async () => {
    setup(true)
    setTargetBudget.mockResolvedValue({})
    fireEvent.click((await screen.findAllByRole('button', { name: 'Budget…' }))[0])
    const form = screen.getByRole('form', { name: 'Budget of dimagi' })
    fireEvent.change(within(form).getByLabelText('sessions a run'), { target: { value: '10' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Save budget' }))
    await waitFor(() => expect(setTargetBudget).toHaveBeenCalledWith('dimagi', 7, expect.objectContaining({
      max_sources_per_run: 10, clear_tokens: true })))
  })

  it('sends a target\'s fade windows by type and its dismissal cooldown, and refuses a malformed map', async () => {
    setup(true)
    setTargetBudget.mockResolvedValue({})
    fireEvent.click((await screen.findAllByRole('button', { name: 'Budget…' }))[0])
    const form = screen.getByRole('form', { name: 'Budget of dimagi' })
    fireEvent.change(within(form).getByLabelText('fade days by finding type'), { target: { value: 'design' } })
    expect((within(form).getByRole('button', { name: 'Save budget' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(within(form).getByLabelText('fade days by finding type'), { target: { value: 'design=120, Friction: 14' } })
    fireEvent.change(within(form).getByLabelText('days a dismissed finding stays dismissed'), { target: { value: '30' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Save budget' }))
    await waitFor(() => expect(setTargetBudget).toHaveBeenCalledWith('dimagi', 7, expect.objectContaining({
      fade_days_by_type: { design: 120, friction: 14 }, dismiss_cooldown_days: 30 })))
  })

  it('shows each target\'s scorecard: weekly bars, precision, open, faded and came back', async () => {
    setup(true)
    const card = await screen.findByRole('region', { name: 'Learner scorecard' })
    await within(card).findByText('50%')
    expect(getBrainScorecard).toHaveBeenCalledWith('dimagi')
    expect(within(card).getByRole('img', { name: /dimagi: findings made in each of the last 8 weeks/ })).toBeTruthy()
    expect(within(card).getByText('6 in 8 weeks')).toBeTruthy()
    expect(within(card).getByText('Dismissed')).toBeTruthy()              // the legend names every fill
  })

  it('draws what a finding reaches and when it was seen, and opens its evidence from the strip', async () => {
    listBrainOutputs.mockResolvedValueOnce([{ ...OUTPUTS[0], sessions_seen: 3, people_seen: 2, breadth: 'pattern',
      came_back: true, recurs_of: 99, sightings: [ago(20), ago(6), ago(1)] }])
    setup(true, '/w/dimagi/brain')
    const card = await openFinding('The runner idles without a delegation')
    expect(within(card).getByText(/canopy · 3 sessions · 2 people · last seen/)).toBeTruthy()
    expect(within(card).getByText('Came back')).toBeTruthy()
    expect(within(card).getByRole('group', { name: 'Seen in 3 sessions with 2 people' })).toBeTruthy()
    expect(within(card).getByRole('img', { name: /^Seen 3 times, first / })).toBeTruthy()
    expect(within(card).queryByText('fourth quote')).toBeNull()
    fireEvent.click(within(card).getByRole('button', { name: '3 sessions' }))
    expect(within(card).getByText('fourth quote')).toBeTruthy()
  })

  it('asks the operator to fix a finding, saying first exactly what that starts and the message it sends', async () => {
    listBrainOutputs.mockResolvedValueOnce([{ ...OUTPUTS[0], fixer: { slug: 'ada', name: 'Ada' }, may_ask_fix: true },
      ...OUTPUTS.slice(1)])
    getFixPlan.mockResolvedValue({ agent: { slug: 'ada', name: 'Ada' }, label: 'Ask Ada to fix this', continues: false,
      what_happens: 'Starts a conversation between you and Ada, with this finding as the brief.',
      brief: 'Jonathan asked you to fix finding 1.' })
    setup(true, '/w/dimagi/brain')
    const card = await openFinding('The runner idles without a delegation')
    expect(within(card).queryByRole('button', { name: 'Accept' })).toBeNull()
    const ask = await within(card).findByRole('button', { name: 'Ask Ada to fix this' })
    expect(getFixPlan).toHaveBeenCalledWith('dimagi', 1)
    expect(within(card).getByText(/Starts a conversation between you and Ada/)).toBeTruthy()
    expect(within(card).getByText('Jonathan asked you to fix finding 1.')).toBeTruthy()
    askFix.mockResolvedValue({ output: { ...OUTPUTS[0], status: 'accepted', fixer: { slug: 'ada', name: 'Ada' },
      fix_session: '/w/dimagi/sessions/f1' }, session_id: 'f1', session_url: 'https://x/w/dimagi/sessions/f1' })
    fireEvent.click(ask)
    await waitFor(() => expect(askFix).toHaveBeenCalledWith('dimagi', 1))
    expect(await within(card).findByText(/Ada is fixing this/)).toBeTruthy()
    expect(within(card).getByRole('link', { name: 'the conversation' }).getAttribute('href')).toBe('/w/dimagi/sessions/f1')
    expect(within(card).getByText('Fixing')).toBeTruthy()
    setOutputStatus.mockResolvedValue({ ...OUTPUTS[0], status: 'done' })
    fireEvent.click(within(card).getByRole('button', { name: 'Mark done' }))
    await waitFor(() => expect(setOutputStatus).toHaveBeenCalledWith('dimagi', 1, 'done'))
  })

  it('says a finding is already fixed or not worth fixing, each choice saying what it does', async () => {
    listBrainOutputs.mockResolvedValueOnce([OUTPUTS[0], { ...OUTPUTS[1], fixer: { slug: 'ada', name: 'Ada' } },
      ...OUTPUTS.slice(2)])
    setup(true, '/w/dimagi/brain')
    const card = await openFinding('A medium one seen lately')
    // An agent may set a status but never asks for a fix itself: it is told who does, and nothing is fetched.
    expect(within(card).queryByRole('button', { name: /fix this/ })).toBeNull()
    expect(within(card).getByText(/for a person who decides on it, signed in as themselves/)).toBeTruthy()
    expect(getFixPlan).not.toHaveBeenCalled()
    expect(within(card).getByText(/Dismisses it\. Learning folds repeats into it/)).toBeTruthy()
    setOutputStatus.mockResolvedValue({ ...OUTPUTS[1], status: 'dismissed' })
    fireEvent.click(within(card).getByRole('button', { name: 'Not worth fixing' }))
    await waitFor(() => expect(setOutputStatus).toHaveBeenCalledWith('dimagi', 2, 'dismissed'))
  })

  it('says why a session is not read, naming no one', async () => {
    setup()
    whyNotRead.mockResolvedValue({ session_id: 'x', learner: 'project-readers', eligible: false,
      reason: 'no_consent:someone', explanation: 'Someone in it has not agreed to canopy learning' })
    const form = await screen.findByRole('form', { name: 'Why is a session not read' })
    fireEvent.change(within(form).getByLabelText('Session'),
      { target: { value: 'https://canopy.dimagi.com/w/dimagi/sessions/0f8fad5b-d9cb-469f-a165-70867728950e' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Look up' }))
    await waitFor(() => expect(whyNotRead).toHaveBeenCalledWith('dimagi', '0f8fad5b-d9cb-469f-a165-70867728950e',
      'project-readers'))
    expect(await screen.findByText(/may not read it: Someone in it has not agreed/)).toBeTruthy()
  })

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
    const consent = screen.getByRole('region', { name: 'Consent' })
    expect(within(consent).getByText('12')).toBeTruthy()
    expect(within(consent).getAllByText('<3')).toHaveLength(3)             // a count under three is never shown
    expect(within(screen.getByRole('region', { name: 'Sources' })).getByText('sensitive 2')).toBeTruthy()
    const links = screen.getByRole('region', { name: 'Stored links' })
    await waitFor(() => expect(within(links).getByRole('link', { name: 'tender work' }).getAttribute('href'))
      .toBe('/w/dimagi/sessions/s1'))
    const runs = screen.getByRole('region', { name: 'Runs' })
    expect(await within(runs).findByText(/\+1 you do not read/)).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'Setup and audit' }).getAttribute('aria-selected')).toBe('true')
    expect(screen.queryByRole('button', { name: 'Run now' })).toBeNull()   // not this workspace's owner
    expect(screen.queryByRole('form', { name: 'Add a target' })).toBeNull()
    const aimed = screen.getByRole('region', { name: 'Targets' })
    expect(within(aimed).getByText('dimagi/supply')).toBeTruthy()            // everyone sees where learners run
    expect(within(aimed).getByText('(paused)', { exact: false })).toBeTruthy()
  })

  it('opens on Findings: no setup reads until the setup tab is chosen', async () => {
    setup(false, '/w/dimagi/brain')
    expect(await screen.findByRole('region', { name: 'Findings' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'Findings' }).getAttribute('aria-selected')).toBe('true')
    expect(listBrainOutputs).toHaveBeenCalledWith('dimagi')
    expect(getBrainState).not.toHaveBeenCalled()
    expect(screen.queryByRole('region', { name: 'Consent' })).toBeNull()
    fireEvent.click(screen.getByRole('tab', { name: 'Setup and audit' }))
    expect(await screen.findByRole('region', { name: 'Consent' })).toBeTruthy()
    expect(getBrainState).toHaveBeenCalledWith('dimagi')
  })

  it('reads an opened finding as what is wrong, the fix, how to tell, and quoted evidence linked to its session', async () => {
    listBrainOutputs.mockResolvedValueOnce([{ ...OUTPUTS[0], may_set_status: false }, ...OUTPUTS.slice(1)])
    setup(false, '/w/dimagi/brain')
    const card = await openFinding('The runner idles without a delegation')
    expect(within(card).getByText('surface the 409')).toBeTruthy()
    expect(within(card).getByText('Belongs in canopy · ada · turn')).toBeTruthy()
    expect(card.textContent).not.toContain('[object Object]')
    expect(card.textContent).not.toMatch(/fix_belongs|suggested_fix|not_checked/)
    // A quote is text, linked to the session its source is, when the viewer reads it; otherwise said plainly.
    expect(within(card).getByText('claim refused: no delegation')).toBeTruthy()
    const links = within(card).getAllByRole('link', { name: 'runner debugging' })
    expect(links[0].getAttribute('href')).toBe('/w/dimagi/sessions/s1')
    expect(within(card).getByText('a session you do not read')).toBeTruthy()
    expect(card.textContent).not.toMatch(/at message \d/)
    // Beyond two quotes, the rest are collapsed.
    expect(within(card).queryByText('third quote')).toBeNull()
    fireEvent.click(within(card).getByRole('button', { name: 'Show 2 more quotes' }))
    expect(within(card).getByText('fourth quote')).toBeTruthy()
    // The rest is behind one disclosure, in words: never a raw record.
    expect(within(card).getByText('How sure: proven')).toBeTruthy()
    expect(within(card).getByText('Merged from: An earlier duplicate')).toBeTruthy()
    expect(card.textContent).not.toMatch(/Key: canopy:x|Id: 8/)
    expect(within(card).getByText(/Found by system-learning in 3 sessions/)).toBeTruthy()
    expect(within(card).getByText('Not checked: whether it is fixed on main')).toBeTruthy()
    expect(within(card).getByText('Nested')).toBeTruthy()
    expect(within(card).getByText('X: 1')).toBeTruthy()
    expect(within(card).getByText('a, b')).toBeTruthy()
    expect(card.textContent).toContain('2 sessions you do not read')
    // Not one of its owners or admins: no buttons, and who decides.
    expect(within(card).queryByRole('button', { name: 'Not worth fixing' })).toBeNull()
    expect(within(card).getByText(/owners and admins decide/)).toBeTruthy()
  })

  it('collapses faded findings in their own group, offers Keep, and never lists a merged duplicate', async () => {
    listBrainOutputs.mockResolvedValueOnce([
      ...OUTPUTS,
      finding(5, { title: 'Nobody saw this lately', faded: true, body: { severity: 'high' } }),
      finding(6, { title: 'A merged duplicate', status: 'duplicate', merged_into: 1 }),
    ])
    setup(true, '/w/dimagi/brain')
    const findings = await screen.findByRole('region', { name: 'Findings' })
    await within(findings).findByText('The runner idles without a delegation')
    expect(within(findings).queryByText('A merged duplicate')).toBeNull()
    const group = within(findings).getByText('Faded: not seen again lately (1)').closest('details')!
    expect(group.open).toBe(false)
    const card = within(group).getByRole('listitem', { name: 'Nobody saw this lately' })
    fireEvent.click(within(card).getByRole('button', { name: /Nobody saw this lately/ }))
    setOutputStatus.mockResolvedValue({ ...finding(5), faded: false })
    fireEvent.click(within(card).getByRole('button', { name: 'Keep' }))
    await waitFor(() => expect(setOutputStatus).toHaveBeenCalledWith('dimagi', 5, 'open'))
  })

  it('filters with dropdowns for status, severity and repo, counted, plus a search, sorted by severity then last seen', async () => {
    setup(false, '/w/dimagi/brain')
    const findings = await screen.findByRole('region', { name: 'Findings' })
    await within(findings).findByText('The runner idles without a delegation')
    const titles = () => within(findings).getAllByRole('heading', { level: 3 }).map((h) => h.textContent)
    // Open and fixing by default: the dismissed one is hidden. High before medium; within high, newest seen first.
    expect(titles()).toEqual(['The runner idles without a delegation', 'A high one seen long ago', 'A medium one seen lately'])
    const status = within(findings).getByLabelText('Status') as HTMLSelectElement
    expect(status.value).toBe('working')
    expect(within(status).getByRole('option', { name: 'Dismissed (1)' })).toBeTruthy()
    fireEvent.change(status, { target: { value: 'all' } })
    expect(titles()).toContain('An old dismissed one')
    fireEvent.change(status, { target: { value: 'working' } })
    const repo = within(findings).getByLabelText('Fix belongs in') as HTMLSelectElement
    expect(within(repo).getByRole('option', { name: 'canopy-web (2)' })).toBeTruthy()
    fireEvent.change(repo, { target: { value: 'canopy-web' } })
    expect(titles()).toEqual(['A high one seen long ago', 'A medium one seen lately'])
    const severity = within(findings).getByLabelText('Severity') as HTMLSelectElement
    expect(within(severity).getByRole('option', { name: 'High (1)' })).toBeTruthy()   // counted under the other filters
    fireEvent.change(severity, { target: { value: 'medium' } })
    expect(titles()).toEqual(['A medium one seen lately'])
    fireEvent.change(severity, { target: { value: '' } })
    fireEvent.change(within(findings).getByLabelText('Search'), { target: { value: 'long ago' } })
    expect(titles()).toEqual(['A high one seen long ago'])
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

  it('lists what a removal could not undo to those who may act on it, and acknowledges it', async () => {
    setup(true)
    acknowledgeFlag.mockResolvedValue(undefined)
    const flags = await screen.findByRole('region', { name: 'Could not be undone' })
    expect(within(flags).getByRole('link', { name: 'https://github.com/dimagi-internal/canopy/pull/77' })).toBeTruthy()
    expect(within(flags).getByText('Pull request')).toBeTruthy()
    fireEvent.click(within(flags).getByRole('button', { name: 'Acknowledge' }))
    await waitFor(() => expect(acknowledgeFlag).toHaveBeenCalledWith('dimagi', 5))
  })

  it('shows no such section when there is nothing to acknowledge', async () => {
    setup()
    await screen.findByRole('region', { name: 'Learners' })
    expect(screen.queryByRole('region', { name: 'Could not be undone' })).toBeNull()
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
