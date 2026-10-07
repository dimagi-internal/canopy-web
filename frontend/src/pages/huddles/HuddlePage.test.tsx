// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
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
  return render(<MemoryRouter initialEntries={['/w/connect/huddles/work-fleet-20261006?view=map']}>
    <Routes><Route path="/w/:workspace/huddles/:id" element={<HuddlePage />} /></Routes></MemoryRouter>)
}

const row = (c: HTMLElement, key: string) => c.querySelector(`[data-message="${key}"]`) as HTMLElement
const open = (c: HTMLElement, key: string) => {
  fireEvent.click(row(c, key).querySelector('button') as HTMLElement)
  return row(c, key).querySelector('[data-detail]') as HTMLElement
}

it('leads with the outcome, then the diagram: one plain line per message', async () => {
  const { container } = renderAt()
  expect(await screen.findByText('What was decided')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Diagram' }).getAttribute('aria-pressed')).toBe('true')
  expect(row(container, 'ask-1').textContent).toContain('Ada · To everyone: What are you working on?')
  expect(row(container, 'reply-eva-1').textContent).toContain('Eva → Ada · Top priority: Connect funder pipeline')
  expect(screen.queryByText('Shipped the Gates trip roster')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Story' }))
  expect(container.querySelector('[data-sequence]')).toBeNull()
  expect(container.querySelector('[data-story]')).toBeTruthy()
})

it('opens one message in place, and closes it again', async () => {
  const { container } = renderAt()
  await screen.findByText('What was decided')
  open(container, 'reply-eva-1')
  expect(screen.getByText('Shipped the Gates trip roster')).toBeTruthy()
  expect(container.querySelectorAll('[data-detail]').length).toBe(1)
  fireEvent.click(row(container, 'reply-eva-1').querySelector('button') as HTMLElement)
  expect(screen.queryByText('Shipped the Gates trip roster')).toBeNull()
})

it("the leader's ask carries the questions it put to each member", async () => {
  const { container } = renderAt()
  await screen.findByText('What was decided')
  expect(row(container, 'ask-2').textContent).toContain('plus 1 question of her own')
  const d = open(container, 'ask-2')
  expect(d.textContent).toContain('To Eva')
  expect(d.textContent).toContain('Is Q4 realistic?')
})

it('says when an answer is still coming, hidden, or was ignored — and why', async () => {
  const { container } = renderAt()
  await screen.findByText('What was decided')
  expect(row(container, 'reply-echo-1').textContent).toContain('Still answering…')
  expect(row(container, 'reply-hal-1').textContent).toContain('Answered (hidden from you)')
  expect(row(container, 'reply-echo-2').textContent).toContain('No answer')
  expect(open(container, 'reply-echo-2').textContent).toMatch(/block names huddle "h0"/)
})

it('ideas, answers and settled changes each read in one line, in full on click', async () => {
  const { container } = renderAt()
  await screen.findByText('What was decided')
  expect(row(container, 'reply-eva-2').textContent).toContain('4 ideas: “Joint Q4 brief”')
  expect(open(container, 'reply-eva-2').querySelectorAll('[data-proposal=""]').length).toBe(4)
  const echo3 = row(container, 'reply-echo-3')
  expect(echo3.querySelector('[data-answer="co-sign"]')).toBeTruthy()
  expect(echo3.querySelector('[data-answer="decline"]')).toBeTruthy()
  expect(row(container, 'reply-hal-3').querySelector('[data-answer="amend"]')).toBeTruthy()
  expect(container.querySelector('[data-step="4"]')?.getAttribute('aria-label')).toBe('Settling changes')
  expect(row(container, 'reply-eva-4').textContent).toContain('Agreed to the changes: “Pipeline sheet”')
  expect(open(container, 'reply-eva-4').querySelector('[data-resolution="accept"]')?.textContent).toContain('weekly works')
  // Not finished: no result arrow yet.
  expect(row(container, 'result')).toBeNull()
})

it('lists outputs under their idea, with live status in plain words', async () => {
  const { container } = renderAt()
  await screen.findByText('What was decided')
  const card = container.querySelector('[data-outcome="filed"]')
  expect(card?.textContent).toContain('Joint Q4 brief')
  expect(card?.textContent).toContain("medium job · 80% sure it's worth it")
  expect(card?.textContent).toContain('waiting for your yes/no')
  expect(screen.getByRole('link', { name: /on Eva's board/ }).getAttribute('href')).toBe('/w/connect/agents/eva/work')
})
