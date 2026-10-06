// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'

// A fixture huddle: two members × three rounds, one joint proposal per co-sign
// state (co-signed / amended / declined / still pending), one in-flight cell, one
// hidden cell and one finished turn whose transcript has aged out.
const huddle = {
  id: 'work-fleet-20261006', type: 'work', team: 'fleet', leader: 'ada', members: ['eva', 'echo', 'hal'],
  anchor_turn_id: 'a', created_at: new Date().toISOString(), finished: false, summary: '',
  deadline_at: null, outcome_count: 1, rounds_dispatched: 4,
  cells: [
    { member: 'eva', round: 1, attempt: 1, turn_id: 't1', status: 'done', created_at: '', finished_at: '',
      content_hidden: false, prompt: 'report', reply_source: 'closeout', reply_error: '', has_transcript: true,
      block: { huddle: 'work-fleet-20261006', round: 1, member: 'eva', worked_on: ['Shipped the Gates trip roster'],
               priorities: ['Connect funder pipeline'] } },
    { member: 'echo', round: 1, attempt: 1, turn_id: 't2', status: 'running', created_at: '', finished_at: null,
      content_hidden: false, prompt: 'report', reply_source: 'none', reply_error: '', has_transcript: false, block: null },
    { member: 'hal', round: 1, attempt: 1, turn_id: 't5', status: 'done', created_at: '', finished_at: '',
      content_hidden: true, prompt: '', reply_source: 'none', reply_error: '', has_transcript: false, block: null },
    { member: 'eva', round: 2, attempt: 2, turn_id: 't4', status: 'done', created_at: '', finished_at: '',
      content_hidden: false, prompt: "ada's questions for you:\nIs Q4 realistic?\n\nPropose", reply_source: 'transcript',
      reply_error: '', has_transcript: true,
      block: { huddle: 'work-fleet-20261006', round: 2, member: 'eva', proposals: [
        { title: 'Joint Q4 brief', lead: 'eva', with: ['echo'], priority: 'Connect funder pipeline', effort: 'M',
          confidence: 0.8, why: 'three funders asked', plan: ['draft', 'review'] },
        { title: 'Pipeline sheet', lead: 'eva', with: ['hal'] },
        { title: 'Funder map', lead: 'eva', with: ['echo'] },
        { title: 'Board digest', lead: 'eva', with: ['hal'] },
      ] } },
    { member: 'echo', round: 2, attempt: 1, turn_id: 't6', status: 'done', created_at: '', finished_at: '',
      content_hidden: false, prompt: '', reply_source: 'none', reply_error: 'block names huddle "h0" round 2',
      has_transcript: false, block: null },
    { member: 'echo', round: 3, attempt: 1, turn_id: 't3', status: 'done', created_at: '', finished_at: '',
      content_hidden: false, prompt: '', reply_source: 'closeout', reply_error: '', has_transcript: false,
      block: { huddle: 'work-fleet-20261006', round: 3, member: 'echo',
               answers: [{ title: 'Joint Q4 brief', lead: 'eva', answer: 'co-sign', note: '' },
                         { title: 'Funder map', lead: 'eva', answer: 'decline', note: 'no capacity' }] } },
    { member: 'hal', round: 3, attempt: 1, turn_id: 't7', status: 'done', created_at: '', finished_at: '',
      content_hidden: false, prompt: '', reply_source: 'closeout', reply_error: '', has_transcript: false,
      block: { huddle: 'work-fleet-20261006', round: 3, member: 'hal',
               answers: [{ title: 'Pipeline sheet', lead: 'eva', answer: 'amend', note: 'weekly, not daily' }] } },
    { member: 'eva', round: 4, attempt: 1, turn_id: 't8', status: 'done', created_at: '', finished_at: '',
      content_hidden: false, prompt: '', reply_source: 'closeout', reply_error: '', has_transcript: false,
      block: { huddle: 'work-fleet-20261006', round: 4, member: 'eva',
               resolutions: [{ title: 'Pipeline sheet', lead: 'eva', resolution: 'accept', note: 'weekly works',
                               proposal: { title: 'Pipeline sheet', lead: 'eva', with: ['hal'], why: 'weekly cadence' } }] } },
  ],
  outputs: [{ agent: 'eva', task_id: 1, ext_id: 'T41', title: 'Joint Q4 brief', status: 'suggested',
              assigned: 'eva', project: 'P3', url: '/w/connect/agents/eva/work' }],
}
vi.mock('@/api/huddles', () => ({ getHuddle: vi.fn(async () => huddle), listHuddles: vi.fn(async () => []) }))
vi.mock('@/components/activity/TurnTranscript', () => ({ TurnTranscript: () => <div>transcript</div> }))
const { HuddlePage } = await import('./HuddlePage')
afterEach(cleanup)

function renderAt() {
  return render(<MemoryRouter initialEntries={['/w/connect/huddles/work-fleet-20261006']}>
    <Routes><Route path="/w/:workspace/huddles/:id" element={<HuddlePage />} /></Routes></MemoryRouter>)
}

it('renders one column per member and the rendered reply, not raw JSON', async () => {
  renderAt()
  expect(await screen.findByText('Shipped the Gates trip roster')).toBeTruthy()
  for (const m of ['eva', 'echo', 'hal']) expect(screen.getAllByText(m).length).toBeGreaterThan(0)
  expect(screen.queryByText(/"worked_on"/)).toBeNull()
})

it('shows a waiting cell for an in-flight member, and status only for a hidden one', async () => {
  renderAt()
  expect((await screen.findAllByText(/waiting/i)).length).toBeGreaterThan(0)
  expect(screen.getByText(/can see that it ran/i)).toBeTruthy()
})

it('says why a reply was ignored', async () => {
  renderAt()
  expect(await screen.findByText(/block names huddle "h0"/)).toBeTruthy()
})

it("shows the leader's critique above the round it shaped", async () => {
  renderAt()
  expect(await screen.findByText('Is Q4 realistic?')).toBeTruthy()
})

it('lists outputs with live status', async () => {
  renderAt()
  expect((await screen.findAllByText('Joint Q4 brief')).length).toBeGreaterThan(0)
  expect(screen.getAllByText('suggested').length).toBeGreaterThan(0)
  expect(screen.getByRole('link', { name: /T41/ }).getAttribute('href')).toBe('/w/connect/agents/eva/work')
})

it('shows every co-sign state, on the answers and the proposals they answer', async () => {
  const { container } = renderAt()
  await screen.findByText('Shipped the Gates trip roster')
  for (const s of ['co-sign', 'amend', 'decline']) {
    expect(container.querySelector(`[data-answer="${s}"]`)).toBeTruthy()
  }
  // The proposer's card shows each partner's answer — pending until it comes.
  const proposals = container.querySelectorAll('[data-proposal=""]')
  expect(proposals.length).toBe(4)
  expect(container.querySelector('[data-partner-state="pending"]')).toBeTruthy()
  expect(container.querySelector('[data-partner-state="co-sign"]')).toBeTruthy()
})

it('names the conditional round 4 and renders its resolutions, turning the accepted amend into a co-sign', async () => {
  const { container } = renderAt()
  await screen.findByText('Shipped the Gates trip roster')
  expect(screen.getAllByText('Resolve').length).toBeGreaterThan(0)
  expect(screen.getByText('Round 4')).toBeTruthy()
  expect(container.querySelector('[data-resolution="accept"]')?.textContent).toContain('weekly works')
  expect(container.querySelector('[data-proposal="revised"]')?.textContent).toContain('weekly cadence')
  // hal's amend on the original card now reads accepted, not amber "amend".
  expect(container.querySelectorAll('[data-partner-state="amend-accepted"]').length).toBeGreaterThan(0)
})
