// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import live from './__fixtures__/work-fleet-20261006.json'

// The page as Jonathan lands on it from the huddle email: the real, filed
// work-fleet-20261006 huddle (3 proposals → 5 board tasks, 2 held).
vi.mock('@/api/huddles', () => ({ getHuddle: vi.fn(async () => live), listHuddles: vi.fn(async () => []) }))
vi.mock('@/components/activity/TurnTranscript', () => ({ TurnTranscript: () => <div>transcript</div> }))
const { HuddlePage } = await import('./HuddlePage')
afterEach(cleanup)

function renderAt() {
  return render(<MemoryRouter initialEntries={['/w/dimagi/huddles/work-fleet-20261006']}>
    <Routes><Route path="/w/:workspace/huddles/:id" element={<HuddlePage />} /></Routes></MemoryRouter>)
}

it('says what was decided, and why 3 proposals made 5 tasks', async () => {
  const { container } = renderAt()
  expect(await screen.findByText(/^3 proposals filed as 5 board tasks — joint proposals put one task on each agent's board\. 2 proposals held\.$/)).toBeTruthy()
  expect(container.querySelector('[data-todo]')?.textContent).toMatch(/5 tasks are waiting for you to accept or decline/)
})

it("groups each proposal's board tasks under it, in plain words", async () => {
  const { container } = renderAt()
  await screen.findByText('What was decided')
  const cards = [...container.querySelectorAll('[data-outcome="filed"]')] as HTMLElement[]
  expect(cards).toHaveLength(3)
  const demo = cards[0]
  expect(within(demo).getByText('Pre-flight one live demo for the IDM talk')).toBeTruthy()
  expect(demo.textContent).toContain('small · 60% confident')
  expect(demo.textContent).toContain('with eva')
  expect(demo.textContent).toContain('Seattle blitz 10/12-14 and the deck due 10/9 (T40/T27; goals sheet Oct set, board, trip-planning state)')
  expect([...demo.querySelectorAll('[data-task]')].map((t) => t.getAttribute('data-task'))).toEqual(['T9', 'T45'])
  expect(within(demo).getByRole('link', { name: /T45 on eva's board/ }).getAttribute('href')).toBe('/w/dimagi/agents/eva/work')
  expect(within(demo).getAllByText('awaiting your accept / decline')).toHaveLength(2)
  expect(container.textContent).not.toMatch(/ball with/)
  expect(cards[2].querySelectorAll('[data-task]')).toHaveLength(1)
})

it('says why each held proposal is held and what would clear it', async () => {
  const { container } = renderAt()
  await screen.findByText('What was decided')
  const held = [...container.querySelectorAll('[data-outcome="held"]')] as HTMLElement[]
  expect(held).toHaveLength(2)
  const idm = held.find((h) => h.textContent?.includes('IDM talk: live demo'))!
  expect(idm.textContent).toContain("echo co-signed with conditions; eva (the lead) hasn't resolved them.")
  expect(idm.textContent).toMatch(/A round 4 would/)
})

it("tucks the leader's email into a disclosure, with its links live", async () => {
  const { container } = renderAt()
  await screen.findByText('What was decided')
  const close = container.querySelector('details[data-close]') as HTMLDetailsElement
  expect(close.open).toBe(false)
  expect(close.textContent).toContain("The email ada sent")
  const link = within(close).getByRole('link', { name: 'https://canopy.dimagi.com/w/dimagi/huddles/work-fleet-20261006' })
  expect(link.getAttribute('href')).toBe('https://canopy.dimagi.com/w/dimagi/huddles/work-fleet-20261006')
  const toggle = screen.getByRole('button', { name: 'Hide the conversation' })
  fireEvent.click(toggle)
  expect(screen.getByRole('button', { name: 'Show the full conversation — 3 rounds, 12 replies' })).toBeTruthy()
})
