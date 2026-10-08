// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { agreed, outOfBudget } from '../huddles/__fixtures__/agreementThreads'
import { ENGINE_TERMS } from '../huddles/plainWords'

const getThread = vi.fn(async (id: string) => (id === agreed.id ? agreed : outOfBudget))
vi.mock('@/api/threads', () => ({ getThread: (id: string) => getThread(id) }))
const { ThreadPage } = await import('./ThreadPage')
afterEach(cleanup)

async function renderAt(id: string) {
  const r = render(<MemoryRouter initialEntries={[`/w/dimagi/threads/${id}`]}>
    <Routes><Route path="/w/:workspace/threads/:id" element={<ThreadPage />} /></Routes></MemoryRouter>)
  await screen.findByRole('heading', { level: 1 })
  return r
}

describe('the thread page', () => {
  it('says what the conversation is for and who is in it', async () => {
    const { container } = await renderAt(agreed.id)
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe("Settle Echo's changes to Eva's IDM talk idea")
    expect([...container.querySelectorAll('[data-participant]')].map((e) => e.textContent))
      .toEqual(['EEva(had the idea)', 'EEcho(asked for changes)'])
    expect(screen.getByText(/2 of 4 messages used/)).toBeTruthy()
    expect(screen.getByRole('link', { name: '← The huddle' }).getAttribute('href')).toBe('/w/dimagi/huddles/work-fleet-20261006')
  })

  it('shows how it ended, with the idea as agreed', async () => {
    const { container } = await renderAt(agreed.id)
    const ended = container.querySelector('[data-outcome]') as HTMLElement
    expect(within(ended).getByText('Agreed')).toBeTruthy()
    expect(within(ended).getByText('Both agree on a 60-second clip.')).toBeTruthy()
    expect(within(ended).getByText('The idea as agreed')).toBeTruthy()
  })

  it('lays the messages out as a conversation, each with where its speaker stands', async () => {
    const { container } = await renderAt(agreed.id)
    const rows = [...container.querySelectorAll('[data-thread-message]')] as HTMLElement[]
    expect(rows).toHaveLength(2)
    expect(within(rows[0]).getByText('Eva')).toBeTruthy()
    expect(within(rows[0]).getByText('to Echo')).toBeTruthy()
    expect(within(rows[0]).getByText('Suggests a change')).toBeTruthy()
    expect(within(rows[0]).getByText('Offers this revised idea')).toBeTruthy()
    expect(within(rows[1]).getByText('Agrees')).toBeTruthy()
    expect(within(rows[1]).getByText('Agreed — under a minute it is.')).toBeTruthy()
    expect(within(rows[1]).getByText('What Echo was asked')).toBeTruthy()
  })

  it('says plainly when it ran out of messages', async () => {
    await renderAt(outOfBudget.id)
    expect(screen.getAllByText('Ran out of messages without agreeing').length).toBeGreaterThan(0)
    expect(screen.getByText('Asks')).toBeTruthy()
    expect(screen.getByText('not agreed')).toBeTruthy()
  })

  it('speaks no engine jargon', async () => {
    const { container } = await renderAt(agreed.id)
    // The verbatim block and prompt sit behind "The full message" / "What … was asked".
    container.querySelectorAll('details').forEach((d) => d.remove())
    const text = container.textContent ?? ''
    for (const re of [...ENGINE_TERMS, /\bround\b/i, /\bthread\b/i]) expect(text).not.toMatch(re)
  })
})
