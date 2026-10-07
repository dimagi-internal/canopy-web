// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import live from './__fixtures__/work-fleet-20261006.json'

// The page as Jonathan lands on it from the huddle email: the real, finished
// work-fleet-20261006 huddle (3 ideas → 5 board tasks, 2 parked), with the
// tasks as they stood on 2026-10-07 — two he declined, three "in progress"
// of which all three are actually stuck (one on him).
vi.mock('@/api/huddles', () => ({ getHuddle: vi.fn(async () => live), listHuddles: vi.fn(async () => []) }))
vi.mock('@/components/activity/TurnTranscript', () => ({ TurnTranscript: () => <div>transcript</div> }))
const { HuddlePage } = await import('./HuddlePage')
afterEach(cleanup)

function renderAt(search = '') {
  return render(<MemoryRouter initialEntries={[`/w/dimagi/huddles/work-fleet-20261006${search}`]}>
    <Routes><Route path="/w/:workspace/huddles/:id" element={<HuddlePage />} /></Routes></MemoryRouter>)
}

it('says what was decided, and why 3 ideas made 5 tasks', async () => {
  renderAt()
  expect(await screen.findByText(
    "3 ideas sent to you to decide, as 5 tasks on the agents' boards (a shared idea puts one task on each agent's board). 2 ideas parked.",
  )).toBeTruthy()
})

it('your move names what is stuck on the reader, not just "in progress"', async () => {
  const { container } = renderAt()
  await screen.findByText('What was decided')
  expect(container.querySelector('[data-todo]')?.textContent).toMatch(
    /One thing is stuck waiting on you \(2 more stuck on something else\)/,
  )
})

it("groups each idea's board tasks under it, each with its status and next step (Full conversation)", async () => {
  const { container } = renderAt('?view=proposal')
  await screen.findByText('What was decided')
  const cards = [...container.querySelectorAll('[data-outcome="filed"]')] as HTMLElement[]
  expect(cards).toHaveLength(3)
  const [demo, diag, alarms] = cards
  expect(within(demo).getByText('Pre-flight one live demo for the IDM talk')).toBeTruthy()
  expect(demo.textContent).toContain("small job · 60% sure it's worth it")
  expect(demo.textContent).toContain('Led by Ace, together with Eva')
  expect(demo.textContent).toContain('Gates/IDM talk Tue Oct 13 11:45am PT + Seattle blitz 10/12-14 and the deck due 10/9')
  expect([...demo.querySelectorAll('[data-task]')].map((t) => t.getAttribute('data-task'))).toEqual(['T9', 'T45'])
  expect(within(demo).getByRole('link', { name: /on Eva's board/ }).getAttribute('href')).toBe('/w/dimagi/agents/eva/work')
  expect(within(demo).getAllByText('you said no')).toHaveLength(2)
  // Declined tasks carry no "next step".
  expect(demo.querySelector('[data-next-step]')).toBeNull()
  // In progress, but stuck — and it says on what.
  const hal = diag.querySelector('[data-task="T50"]') as HTMLElement
  expect(hal.hasAttribute('data-stuck')).toBe(true)
  expect(hal.querySelector('[data-next-step]')?.textContent).toBe(
    'Stuck: The Salesforce credentials and the Google Drive key must be installed on the cloud runner. Dependencies already fixed.',
  )
  expect(diag.querySelector('[data-task="T46"] [data-next-step]')?.textContent).toMatch(/^Stuck: Waiting on Hal first/)
  expect(alarms.querySelector('[data-task="T51"] [data-next-step]')?.textContent).toMatch(/Needs a laptop runner or you/)
})

it('says why each parked idea is parked and what would un-park it', async () => {
  const { container } = renderAt('?view=agent')
  await screen.findByText('What was decided')
  const held = [...container.querySelectorAll('[data-outcome="held"]')] as HTMLElement[]
  expect(held).toHaveLength(2)
  const idm = held.find((h) => h.textContent?.includes('IDM talk: live demo'))!
  expect(idm.textContent).toContain("Echo said yes with changes, and Eva (who suggested it) hasn't confirmed them yet.")
  expect(idm.textContent).toContain("Eva agreeing to Echo's changes would send it to you.")
})

it("keeps the leader's email, word for word, in the Full conversation — not the Story", async () => {
  const { container } = renderAt('?view=agent')
  await screen.findByText('What was decided')
  const close = container.querySelector('details[data-close]') as HTMLDetailsElement
  expect(close.open).toBe(false)
  expect(close.textContent).toContain('The email Ada sent you, word for word')
  const link = within(close).getByRole('link', { name: 'https://canopy.dimagi.com/w/dimagi/huddles/work-fleet-20261006' })
  expect(link.getAttribute('href')).toBe('https://canopy.dimagi.com/w/dimagi/huddles/work-fleet-20261006')
  cleanup()
  const story = renderAt()
  await screen.findByText('What was decided')
  expect(story.container.querySelector('details[data-close]')).toBeNull()
})
