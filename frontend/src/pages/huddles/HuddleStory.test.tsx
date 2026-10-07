// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import live from './__fixtures__/work-fleet-20261006.json'
import { ENGINE_TERMS } from './plainWords'

// The default view, on the real work-fleet-20261006 huddle.
vi.mock('@/api/huddles', () => ({ getHuddle: vi.fn(async () => live), listHuddles: vi.fn(async () => []) }))
vi.mock('@/components/activity/TurnTranscript', () => ({ TurnTranscript: () => <div>transcript</div> }))
const { HuddlePage } = await import('./HuddlePage')
afterEach(cleanup)

async function renderPage() {
  const r = render(<MemoryRouter initialEntries={['/w/dimagi/huddles/work-fleet-20261006']}>
    <Routes><Route path="/w/:workspace/huddles/:id" element={<HuddlePage />} /></Routes></MemoryRouter>)
  await screen.findByText('What was decided')
  return r
}

describe('the Story (default view)', () => {
  it('explains the huddle once, under the title', async () => {
    await renderPage()
    expect(screen.getByRole('heading', { level: 1 }).textContent).toMatch(/^Ada's huddle/)
    expect(screen.getByText('Ada checked in with the agents to work out what they could push forward for you, alone or together.')).toBeTruthy()
  })

  it('reads as three numbered steps, then the result', async () => {
    const { container } = await renderPage()
    const steps = [...container.querySelectorAll('[data-story] > [data-step]')].map((s) => s.getAttribute('data-step'))
    expect(steps).toEqual(['1', '2', '3', 'result'])
    const titles = [...container.querySelectorAll('[data-story] > [data-step] > div > h2')].map((h) => h.textContent)
    expect(titles).toEqual(["Step 1: What everyone's working on", 'Step 2: Ideas', "Step 3: Who's in", 'The result'])
  })

  it("step 1: one line per agent — what it did and what it thinks the top priority is", async () => {
    const { container } = await renderPage()
    const eva = container.querySelector('[data-report="eva"]') as HTMLElement
    expect(eva.textContent).toContain('Thinks the top priority is: Gates/IDM talk Tue Oct 13 11:45am PT + Seattle blitz 10/12-14 and the deck due 10/9.')
    const ace = container.querySelector('[data-report="ace"]') as HTMLElement
    expect(ace.textContent).toContain('Has been working on: Phase 7 convergence: build QA + pre-render arc check so DDD stops catching build output.')
    // The whole answer is one click away.
    expect(within(ace).getByText("Read Ace's full answer")).toBeTruthy()
    expect(ace.textContent).toContain('Spark facilitator programme cascade demo')
  })

  it('step 2: one card per idea, as a sentence, with the questions and answers tucked in', async () => {
    const { container } = await renderPage()
    const ideas = [...container.querySelectorAll('[data-idea]')] as HTMLElement[]
    expect(ideas.map((i) => i.getAttribute('data-idea'))).toEqual([
      'Pre-flight one live demo for the IDM talk',
      'Take PRIDE cholera story to reviewed draft',
      'IDM talk: live demo from Ace, story slide from Echo',
      'Diagnose chrome-sales MCP connect failures on cloud-ec2-2',
      'Deploy connect-labs ALB 5xx alarms, then bound web tier',
    ])
    expect(ideas[2].textContent).toMatch(/^Eva suggested:.*IDM talk: live demo from Ace, story slide from Echo.*together with Ace and Echo/)
    expect(ideas[3].textContent).toContain('with Hal leading, together with Eva')
    expect(ideas[4].textContent).toContain('on its own')
    expect(ideas[0].textContent).toContain("Why: Eva's deck needs demo picks by 10/9.")
    // Ada's questions to Eva, paired with Eva's answers — shown once per author.
    expect(within(ideas[2]).getByText("Ada's questions to Eva and the answers")).toBeTruthy()
    expect(ideas[2].querySelectorAll('[data-qa-pair]')).toHaveLength(3)
    expect(within(ideas[3]).queryByText(/questions to Eva/)).toBeNull()
  })

  it("step 3: Ada's take, then each teammate's answer as a pill and a sentence", async () => {
    const { container } = await renderPage()
    const idm = container.querySelector('[data-whos-in="IDM talk: live demo from Ace, story slide from Echo"]') as HTMLElement
    expect(idm.textContent).toContain("Ada's take: Strongest proposal")
    const echo = idm.querySelector('[data-answer-of="echo"]') as HTMLElement
    expect(echo.querySelector('[data-answer="amend"]')?.textContent).toBe('In, with changes')
    expect(echo.textContent).toContain('Echo:In, with changesI sign on with two changes.')
    expect(idm.querySelector('[data-answer-of="ace"] [data-answer="co-sign"]')?.textContent).toBe("I'm in")
    const solo = container.querySelector('[data-whos-in="Deploy connect-labs ALB 5xx alarms, then bound web tier"]') as HTMLElement
    expect(solo.textContent).toContain('Hal takes this on alone')
  })

  it('links one idea down the page: same letter and colour in steps 2, 3 and the result', async () => {
    const { container } = await renderPage()
    const title = 'Diagnose chrome-sales MCP connect failures on cloud-ec2-2'
    const cards = [
      container.querySelector(`[data-idea="${title}"]`),
      container.querySelector(`[data-whos-in="${title}"]`),
      [...container.querySelectorAll('[data-result]')].find((r) => r.textContent?.includes(title)),
    ] as HTMLElement[]
    const stripes = cards.map((c) => c.style.borderLeftColor)
    expect(new Set(stripes).size).toBe(1)
    const letters = cards.map((c) => c.querySelector('h3 [aria-hidden]')?.textContent)
    expect(letters).toEqual(['D', 'D', 'D'])
  })

  it('the result: what was sent with who does what and where it stands; what was parked and why', async () => {
    const { container } = await renderPage()
    const result = container.querySelector('#huddle-result') as HTMLElement
    expect(within(result).getByText('Sent to you to decide · 3')).toBeTruthy()
    expect(within(result).getByText('Parked · 2')).toBeTruthy()
    const sent = [...result.querySelectorAll('[data-result="sent"]')] as HTMLElement[]
    expect(sent[0].textContent).toContain('Ace leads it, together with Eva.')
    expect(within(sent[0]).getByRole('link', { name: /on Ace's board/ })).toBeTruthy()
    expect(within(sent[0]).getAllByText('you said no')).toHaveLength(2)
    expect(result.querySelector('[data-task="T51"] [data-next-step]')?.textContent).toMatch(/^Stuck: Alarms can't be checked from the cloud runner/)
    const parked = [...result.querySelectorAll('[data-result="parked"]')] as HTMLElement[]
    expect(parked.map((p) => p.textContent).join(' ')).toContain(
      "Eva and Ace said yes with changes, and Echo (who suggested it) hasn't confirmed them yet.",
    )
  })

  it('never shows an engine term — anywhere on the default view', async () => {
    await renderPage()
    const text = document.body.textContent ?? ''
    for (const re of ENGINE_TERMS) expect(text).not.toMatch(re)
    // Nor ticket ids as link text, nor internal noise.
    expect(text).not.toMatch(/\bT(9|45|46|50|51) on\b/)
    expect(text).not.toMatch(/reply_source|rounds dispatched|in_progress/)
  })
})
