// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import live from './__fixtures__/work-fleet-20261006.json'
import { threads } from './__fixtures__/agreementThreads'

// The real work-fleet-20261006 huddle, with two agreement threads hanging off it:
// Eva and Echo agreed Echo's changes directly; Echo and Ace ran out of messages.
vi.mock('@/api/huddles', () => ({ getHuddle: vi.fn(async () => live), listHuddles: vi.fn(async () => []) }))
vi.mock('@/api/threads', () => ({ getHuddleThreads: vi.fn(async () => threads) }))
vi.mock('@/components/activity/TurnTranscript', () => ({ TurnTranscript: () => <div>transcript</div> }))
const { HuddlePage } = await import('./HuddlePage')
afterEach(cleanup)

async function renderAt(search = '') {
  const r = render(<MemoryRouter initialEntries={[`/w/dimagi/huddles/work-fleet-20261006${search}`]}>
    <Routes><Route path="/w/:workspace/huddles/:id" element={<HuddlePage />} /></Routes></MemoryRouter>)
  await screen.findByText('What was decided')
  return r
}

describe('agreement threads on the huddle page', () => {
  it('the Story tells each direct conversation and links to it', async () => {
    const { container } = await renderAt()
    const idm = container.querySelector('[data-whos-in^="IDM talk"]') as HTMLElement
    expect(within(idm).getByText('Changes agreed')).toBeTruthy()
    expect(idm.querySelector('[data-thread="thr-aaaaaaaaaaaa"]')?.textContent)
      .toMatch(/^Then Eva and Echo talked the changes through directly: Agreed — Both agree on a 60-second clip\./)
    expect(within(idm).getByRole('link', { name: 'Read the conversation →' }).getAttribute('href'))
      .toBe('/w/dimagi/threads/thr-aaaaaaaaaaaa')
    const pride = container.querySelector('[data-whos-in^="Take PRIDE"]') as HTMLElement
    expect(pride.querySelector('[data-thread="thr-bbbbbbbbbbbb"]')?.textContent).toMatch(/Ran out of messages without agreeing/)
    expect(screen.getByText(/the idea's lead and that teammate then talked it through directly/)).toBeTruthy()
  })

  it('the Diagram adds step 4 with direct arrows between the two agents', async () => {
    const { container } = await renderAt('?view=map')
    const four = container.querySelector('[data-sequence] [data-step="4"]') as HTMLElement
    expect(four.getAttribute('aria-label')).toBe('Settling changes')
    expect(within(four).getByText('Step 4 · Settling changes')).toBeTruthy()
    const rows = [...four.querySelectorAll('[data-message]')] as HTMLElement[]
    expect(rows.map((r) => r.getAttribute('data-kind'))).toEqual(['direct', 'direct', 'direct', 'settled', 'direct', 'settled'])
    expect(rows[0].querySelector('[data-arrow="direct"]')).toBeTruthy()
    expect(rows[0].textContent).toMatch(/^.*Eva → Echo · Suggests a change: A clip works for me/)
    fireEvent.click(rows[0].querySelector('button')!)
    const link = within(container.querySelector('[data-detail]') as HTMLElement).getByRole('link', { name: 'Open the whole conversation' })
    expect(link.getAttribute('href')).toBe('/w/dimagi/threads/thr-aaaaaaaaaaaa')
    expect(screen.getByText('two agents talk directly')).toBeTruthy()
  })
})

describe('what was decided', () => {
  it('links each idea to the direct conversation that settled its changes', async () => {
    const { container } = await renderAt('?view=agent')
    const line = container.querySelector('[data-conversation="thr-aaaaaaaaaaaa"]') as HTMLElement
    expect(line.textContent).toBe("Eva and Echo agreed Echo's changes directly — the conversation")
    expect(within(line).getByRole('link').getAttribute('href')).toBe('/w/dimagi/threads/thr-aaaaaaaaaaaa')
    expect(container.querySelector('[data-conversation="thr-bbbbbbbbbbbb"]')?.textContent)
      .toBe("Echo and Ace talked Ace's changes through directly but didn't agree — the conversation")
  })
})
