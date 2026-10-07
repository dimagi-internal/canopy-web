// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Huddle } from '@/api/huddles'
import live from './__fixtures__/work-fleet-20261006.json'
import { pairQA, proposalThreads, repliesOf, threadFor } from './conversationModel'
import { leaderAsks, type Block } from './huddleModel'

vi.mock('@/components/activity/TurnTranscript', () => ({ TurnTranscript: () => <div>transcript</div> }))
const { HuddleConversation } = await import('./HuddleConversation')
afterEach(cleanup)

const h = live as unknown as Huddle
const cell = (m: string, r: number) => h.cells.find((c) => c.member === m && c.round === r)!

function Where() {
  const l = useLocation()
  return <output data-testid="where">{l.search}</output>
}

function renderAt(search = '') {
  return render(
    <MemoryRouter initialEntries={[`/w/dimagi/huddles/work-fleet-20261006${search}`]}>
      <Routes>
        <Route path="/w/:workspace/huddles/:id" element={<><HuddleConversation huddle={h} /><Where /></>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('the model', () => {
  it("pairs ada's round-2 questions with eva's answers, in order", () => {
    const { paired, rows } = pairQA(leaderAsks(cell('eva', 2).prompt, 'ada'), cell('eva', 2).block as Block)
    expect(paired).toBe(true)
    expect(rows).toHaveLength(3)
    expect(rows[0].question).toMatch(/^You are the only member that read Jonathan's goals/)
    expect(rows[0].answer).toMatch(/^Goal contents are restricted/)
    expect(rows[2].question).toMatch(/blocked on humans/)
    expect(rows[2].answer).toMatch(/^T36 Neal nudge send OK/)
  })

  it('falls back to titled answers when the counts differ', () => {
    const { paired, rows } = pairQA([{ about: '', text: 'only one' }], cell('eva', 2).block as Block)
    expect(paired).toBe(false)
    expect(rows.map((r) => r.question)).toEqual(['', '', ''])
    expect(rows[0].title).toBe('Oct goals other agents could move this week')
  })

  it("matches round-3 answers to proposals — hal's untitled answer to the proposal he was named lead of", () => {
    const r = repliesOf(h)
    const hal = r.find((x) => x.member === 'hal')!
    expect(hal.loose).toBe(true)
    expect(hal.pitch?.title).toBe('Diagnose chrome-sales MCP connect failures on cloud-ec2-2')
    const onIdm = r.filter((x) => x.pitch?.title === 'IDM talk: live demo from Ace, story slide from Echo')
    expect(onIdm.map((x) => [x.member, x.answer])).toEqual([['ace', 'co-sign'], ['echo', 'amend']])
  })

  it("eva's thread: inbound answers to her proposals in round 3", () => {
    const t = threadFor(h, 'eva', [1, 2, 3])
    expect(t[2].joint.map((j) => j.title)).toEqual([
      'Pre-flight one live demo for the IDM talk',
      'Take PRIDE cholera story to reviewed draft',
      'Diagnose chrome-sales MCP connect failures on cloud-ec2-2',
    ])
    expect(t[2].joint[0].critique).toMatch(/^This is the same work as Eva's/)
    expect(t[2].inbound.map((x) => x.member)).toEqual(['ace', 'echo', 'hal'])
    expect(t[2].ownQuestions[0]).toMatch(/^You gave goal THEMES only/)
  })

  it('proposal threads: filed first, then held, each with its critique', () => {
    const ts = proposalThreads(h)
    const titles = ts.map((t) => [t.outcome.title, t.outcome.verdict])
    expect(titles.slice(0, 3).every(([, v]) => v === 'filed')).toBe(true)
    expect(titles.slice(3).every(([, v]) => v === 'held')).toBe(true)
    const idm = ts.find((t) => t.outcome.title.startsWith('IDM talk'))!
    expect(idm.critique).toMatch(/^Strongest proposal/)
    expect(idm.replies.map((r) => r.member)).toEqual(['ace', 'echo'])
  })
})

describe('By agent (Full conversation)', () => {
  it("opens on the first member's thread and reads it top to bottom", async () => {
    const { container } = renderAt('?view=agent')
    expect(screen.getByRole('button', { name: 'Full conversation' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: 'By agent' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: /^(A\s*)?Ace$/ }).getAttribute('aria-pressed')).toBe('true')
    expect(container.querySelector('[data-thread="ace"]')).toBeTruthy()
    for (const r of ["Step 1 · What everyone's working on", 'Step 2 · Ideas', "Step 3 · Who's in"]) {
      expect(screen.getByRole('heading', { name: r })).toBeTruthy()
    }
  })

  it("shows ada's real round-2 questions to eva, each followed by eva's answer", () => {
    const { container } = renderAt('?view=agent&with=eva')
    const rows = container.querySelectorAll('[data-round="2"] [data-qa-row]')
    expect(rows).toHaveLength(3)
    expect(rows[1].textContent).toContain('Ada asked: The IDM deck (T40, due 10/9) has open demo picks and charts')
    expect(rows[1].textContent).toContain('Ace: pick 1-2 existing demos (Spark cascade run 20261004-1706')
    // ada's own bubble lists the same questions in full.
    const ask = container.querySelector('[data-bubble="leader-2"]') as HTMLElement
    expect(ask.textContent).toContain('Several of your items are blocked on humans (Beth, Neal, Jonathan).')
    // And her proposals render as readable cards, not JSON.
    expect(within(container.querySelector('[data-bubble="eva-2-proposal-0"]') as HTMLElement).getByText('IDM talk: live demo from Ace, story slide from Echo')).toBeTruthy()
    // The reply is rendered, not dumped (only "see full message" holds raw text).
    for (const k of ['eva-2-proposal-0', 'eva-2-qa-0']) {
      expect(container.querySelector(`[data-bubble="${k}"]`)?.textContent).not.toContain('"title"')
    }
  })

  it("round 3 pairs each critique with eva's answer, then teammates' answers to her proposals", () => {
    const { container } = renderAt('?view=agent&with=eva')
    const order = [...container.querySelectorAll('[data-round="3"] [data-bubble]')].map((b) => b.getAttribute('data-bubble'))
    expect(order).toEqual([
      'leader-3', 'leader-3-critique-0', 'eva-3-answer-0', 'leader-3-critique-1', 'eva-3-answer-1',
      'leader-3-critique-2', 'eva-3-answer-2', 'leader-3-own', 'eva-3-also', 'inbound-ace', 'inbound-echo', 'inbound-hal',
    ])
    expect(container.querySelector('[data-bubble="leader-3-critique-0"]')?.textContent).toContain("This is the same work as Eva's")
  })

  it("puts teammates' answers to eva's proposals in eva's thread", () => {
    const { container } = renderAt('?view=agent&with=eva')
    const echo = container.querySelector('[data-bubble="inbound-echo"]') as HTMLElement
    expect(echo.textContent).toContain("Echo on your idea ‘IDM talk: live demo from Ace, story slide from Echo’")
    expect(echo.querySelector('[data-answer="amend"]')).toBeTruthy()
    expect(echo.textContent).toContain('the slide uses ONLY already-public material')
    expect(container.querySelector('[data-bubble="inbound-ace"] [data-answer="co-sign"]')).toBeTruthy()
  })

  it('renders full text — no ellipsis truncation of long replies', () => {
    const { container } = renderAt('?view=agent&with=eva')
    const note = (cell('eva', 3).block as { answers: { note: string }[] }).answers[0].note
    expect(note.length).toBeGreaterThan(300)
    expect(container.querySelector('[data-bubble="eva-3-answer-0"]')?.textContent).toContain(note)
    const priority = (cell('eva', 1).block as { priorities: string[] }).priorities[0]
    expect(container.querySelector('[data-bubble="eva-1-priorities"]')?.textContent).toContain(priority)
  })

  it('switching member updates ?with=, and ?with= picks the member', () => {
    renderAt('?view=agent&with=hal')
    expect(screen.getByRole('button', { name: /^(H\s*)?Hal$/ }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: /^(E\s*)?Echo$/ }))
    expect(screen.getByTestId('where').textContent).toContain('with=echo')
    expect(screen.getByRole('button', { name: /^(E\s*)?Echo$/ }).getAttribute('aria-pressed')).toBe('true')
  })
})

describe('By idea and the switcher', () => {
  it('pitch → critique → responses → outcome, per proposal', () => {
    renderAt('?view=proposal')
    expect(screen.getByRole('button', { name: 'By idea' }).getAttribute('aria-pressed')).toBe('true')
    const thread = document.querySelector('[data-proposal-thread="IDM talk: live demo from Ace, story slide from Echo"]') as HTMLElement
    const order = [...thread.querySelectorAll('[data-bubble]')].map((b) => b.getAttribute('data-bubble'))
    expect(order).toEqual(['pitch', 'critique', 'reply-ace', 'reply-echo'])
    expect(thread.querySelector('[data-bubble="critique"]')?.textContent).toContain('Strongest proposal: hard date')
    expect(thread.querySelector('[data-outcome-footer="held"]')?.textContent).toMatch(/Parked/)
    const filed = document.querySelector('[data-proposal-thread="Pre-flight one live demo for the IDM talk"] [data-outcome-footer="filed"]') as HTMLElement
    expect(within(filed).getByRole('link', { name: "on Ace's board" }).getAttribute('href')).toBe('/w/connect/agents/ace/work')
    expect(within(filed).getByRole('link', { name: "on Eva's board" })).toBeTruthy()
    // Declined by the reader, said plainly; no ticket ids in the words.
    expect(filed.textContent).toContain('you said no')
    expect(filed.textContent).not.toMatch(/\bT9\b|\bT45\b/)
    // A task that reads "in progress" but is stuck says so.
    const diag = document.querySelector('[data-proposal-thread="Diagnose chrome-sales MCP connect failures on cloud-ec2-2"] [data-task="T50"]') as HTMLElement
    expect(diag.textContent).toMatch(/in progress — Stuck: The Salesforce credentials and the Google Drive key must be installed on the cloud runner/)
  })

  it('names a lead the proposer picked', () => {
    renderAt('?view=proposal')
    const t = document.querySelector('[data-proposal-thread="Diagnose chrome-sales MCP connect failures on cloud-ec2-2"]') as HTMLElement
    expect(t.textContent).toContain('suggested by Eva, with Hal leading')
    expect(t.querySelector('[data-bubble="reply-hal"]')?.textContent).toContain('could not see the title')
  })

  it('opens on the Story; the switcher writes ?view= and back', async () => {
    const { container } = renderAt()
    expect(screen.getByRole('button', { name: 'Story' }).getAttribute('aria-pressed')).toBe('true')
    expect(container.querySelector('[data-story]')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Diagram' }))
    expect(screen.getByTestId('where').textContent).toContain('view=map')
    expect(screen.getByRole('button', { name: 'Diagram' }).getAttribute('aria-pressed')).toBe('true')
    expect(container.querySelector('[data-card="eva-1"]')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Full conversation' }))
    expect(screen.getByTestId('where').textContent).toContain('view=agent')
    fireEvent.click(screen.getByRole('button', { name: 'By idea' }))
    expect(screen.getByTestId('where').textContent).toContain('view=proposal')
    // From the Diagram, Full conversation opens By agent; Story drops ?view= (the default).
    fireEvent.click(screen.getByRole('button', { name: 'Diagram' }))
    fireEvent.click(screen.getByRole('button', { name: 'Full conversation' }))
    expect(screen.getByTestId('where').textContent).toContain('view=agent')
    fireEvent.click(screen.getByRole('button', { name: 'Story' }))
    expect(screen.getByTestId('where').textContent).not.toContain('view=')
    expect(container.querySelector('[data-story]')).toBeTruthy()
  })

  it('a ?view= link opens that view', () => {
    renderAt('?view=map')
    expect(screen.getByRole('button', { name: 'Diagram' }).getAttribute('aria-pressed')).toBe('true')
  })
})
