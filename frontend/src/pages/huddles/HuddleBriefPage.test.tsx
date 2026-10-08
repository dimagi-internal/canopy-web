// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'

// A huddle run on a priorities brief (canopy `huddle plan --priorities-file`,
// 2026-10-09 on): round 1 reports levers, an idea names its priority by number,
// says what it needs from Jonathan and what would make it fail — no confidence %.
const H = 'work-fleet-20261009'
const brief = [
  '1. Land two Q4 funder commitments — hard dates: 2026-10-20 — source: goals sheet',
  '2. IDM keynote lands — hard dates: 2026-10-15 — source: calendar',
  'Not now: new product lines',
].join('\n')
const cell = (member: string, round: number, block: Record<string, unknown>) => ({
  member, round, attempt: 1, turn_id: `${member}-${round}`, status: 'done', created_at: '', finished_at: '',
  content_hidden: false, prompt: 'p', reply_source: 'closeout', reply_error: '', has_transcript: false,
  block: { huddle: H, round, member, ...block },
})
const huddle = {
  id: H, type: 'work', team: 'fleet', leader: 'ada', members: ['ace', 'echo'], anchor_turn_id: 'a',
  created_at: new Date().toISOString(), finished: false, summary: '', priorities_brief: brief,
  deadline_at: null, outcome_count: 0, rounds_dispatched: 3,
  cells: [
    cell('ace', 1, { state: ['T51 alarms half done'],
                     levers: [{ priority: 2, move: 'Rehearse the live demo', kind: 'new', task: '', blocked_by: '', verified: true }],
                     offers: [], needs: [{ from: 'jonathan', ask: 'Pick the demo flow' }] }),
    cell('echo', 1, { state: [], levers: [], offers: [], needs: [] }),
    cell('ace', 2, { proposals: [{
      title: 'Pre-flight the IDM live demo', lead: 'ace', with: ['echo'], priority: 2, kind: 'new',
      project: { name: 'IDM talk', new: false }, why: 'Talk is 10/15 (checked)', plan: ['2026-10-09: script'],
      effort: 'S', success_measure: 'dry run passes', cost_to_jonathan: { kind: 'time', detail: '20-minute dry run Tuesday' },
      fails_if: 'the demo env flakes on stage', ask_of_partners: { echo: 'story slide' } }] }),
    cell('echo', 3, { answers: [{ title: 'Pre-flight the IDM live demo', lead: 'ace', answer: 'co-sign', note: '' }] }),
  ],
  outputs: [],
}
vi.mock('@/api/huddles', () => ({ getHuddle: vi.fn(async () => huddle), listHuddles: vi.fn(async () => []) }))
vi.mock('@/components/activity/TurnTranscript', () => ({ TurnTranscript: () => <div>transcript</div> }))
const { HuddlePage } = await import('./HuddlePage')
afterEach(cleanup)

function renderAt(view: string) {
  return render(<MemoryRouter initialEntries={[`/w/connect/huddles/${H}?view=${view}`]}>
    <Routes><Route path="/w/:workspace/huddles/:id" element={<HuddlePage />} /></Routes></MemoryRouter>)
}

it("shows Jonathan's priorities, numbered, with their dates", async () => {
  const { container } = renderAt('map')
  await screen.findByText('What was decided')
  const panel = container.querySelector('[data-brief]') as HTMLElement
  expect(panel.textContent).toContain("Jonathan's priorities for this huddle")
  expect(panel.querySelector('[data-priority="2"]')?.textContent).toBe('2IDM keynote lands · by 2026-10-15')
  expect(panel.textContent).toContain('Not now: new product lines')
})

it('an agreed idea names its priority, what it needs from you and what would sink it — no %', async () => {
  const { container } = renderAt('map')
  await screen.findByText('What was decided')
  const card = container.querySelector('[data-outcome="filed"]') as HTMLElement
  expect(card.textContent).toContain('Serves priority 2: IDM keynote lands')
  expect(card.querySelector('[data-needs-you]')?.textContent).toBe('Needs from you: 20-minute dry run Tuesday')
  expect(card.querySelector('[data-fails-if]')?.textContent).toBe('Would fail if the demo env flakes on stage')
  expect(card.textContent).not.toMatch(/% sure/)
})

it("a report's levers read as lines, and the diagram says which priority a member can move", async () => {
  const { container } = renderAt('map')
  await screen.findByText('What was decided')
  const ace1 = container.querySelector('[data-message="reply-ace-1"]') as HTMLElement
  expect(ace1.textContent).toContain('Can move priority 2: Rehearse the live demo')
  fireEvent.click(ace1.querySelector('button') as HTMLElement)
  const detail = ace1.querySelector('[data-detail]') as HTMLElement
  expect(detail.textContent).toContain('Priority 2 (IDM keynote lands): Rehearse the live demo — new work · checked')
  expect(detail.textContent).toContain('From Jonathan: Pick the demo flow')
  expect(detail.textContent).not.toContain('{"')
})

it('the Story tells step 1 as working toward the brief', async () => {
  const { container } = renderAt('story')
  await screen.findByText('What was decided')
  expect(container.querySelector('[data-story]')?.textContent).toContain("Ada gave each agent Jonathan's priorities and asked where it could move them.")
  expect(container.querySelector('[data-report="ace"]')?.textContent).toContain('Can move: Priority 2 (IDM keynote lands): Rehearse the live demo')
})
