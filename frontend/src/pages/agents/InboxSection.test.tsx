// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * The inbox holds two shapes of wait, and both are real:
 *
 *   * an ASK — a review or question, decidable in place, and
 *   * a task PARKED ON YOU, where nothing is being asked but the next step is
 *     yours. That is most of what the fleet's boards actually hold, and it
 *     reached no inbox at all until a task could name a person.
 */

const listItems = vi.fn()
const listWaitingTasks = vi.fn()

vi.mock('@/api/items', () => ({ listItems }))
vi.mock('@/api/agents', () => ({ listWaitingTasks }))
vi.mock('@/components/items/ItemCard', () => ({
  ItemCard: ({ item }: { item: { title: string } }) => <div>{item.title}</div>,
}))
vi.mock('@/widget/usePageState', () => ({ usePageState: () => {} }))
vi.mock('@/widget/useResource', () => ({ useResource: () => {} }))

const agent = { slug: 'eva', name: 'Eva' }
vi.mock('react-router-dom', () => ({ useOutletContext: () => ({ agent }) }))

const { InboxSection } = await import('./InboxSection')

function ask(over: Record<string, unknown> = {}) {
  return {
    id: 'ask-1',
    agent_slug: 'eva',
    kind: 'review',
    title: 'Send the EOI?',
    body: '',
    state: 'open',
    created_at: '2026-09-01T00:00:00Z',
    ...over,
  }
}

function parked(over: Record<string, unknown> = {}) {
  return {
    id: 7,
    ext_id: 'T4',
    uuid: 'task-uuid-1',
    agent_slug: 'eva',
    title: 'EOI numbers',
    next_action: 'Chase Andrea for the 7 blank cells',
    ask_kind: '',
    ask_state: '',
    project_name: 'Coefficient Giving EOI',
    status: 'in_progress',
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    ...over,
  }
}

beforeEach(() => {
  listItems.mockResolvedValue([])
  listWaitingTasks.mockResolvedValue([])
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('the agent inbox', () => {
  it('shows a task parked on you, with its next step and project', async () => {
    listWaitingTasks.mockResolvedValue([parked()])
    render(<InboxSection />)

    const row = await screen.findByTestId('waiting-T4')
    expect(row.textContent).toContain('EOI numbers')
    expect(row.textContent).toContain('Chase Andrea for the 7 blank cells')
    expect(row.textContent).toContain('Coefficient Giving EOI')
  })

  it('counts asks and parked tasks together in the badge', async () => {
    listItems.mockResolvedValue([ask()])
    listWaitingTasks.mockResolvedValue([parked()])
    render(<InboxSection />)

    expect(await screen.findByText('2 waiting on you')).toBeTruthy()
  })

  it('shows an ask that also names a person ONCE', async () => {
    // Same wait, two sources. Counting it twice would make the badge lie.
    listItems.mockResolvedValue([ask({ id: 'shared-uuid' })])
    listWaitingTasks.mockResolvedValue([
      parked({ uuid: 'shared-uuid', ask_kind: 'review', ask_state: 'open' }),
    ])
    render(<InboxSection />)

    expect(await screen.findByText('1 waiting on you')).toBeTruthy()
    expect(screen.queryByTestId('waiting-T4')).toBeNull()
  })

  it('says the agent has the ball when nothing waits', async () => {
    render(<InboxSection />)

    expect(await screen.findByText(/has the ball/)).toBeTruthy()
  })

  it('does not claim an empty inbox while it is still loading', async () => {
    let resolve: (v: unknown) => void = () => {}
    listItems.mockReturnValue(new Promise((r) => { resolve = r }))
    render(<InboxSection />)

    expect(screen.queryByText(/has the ball/)).toBeNull()

    resolve([])
    expect(await screen.findByText(/has the ball/)).toBeTruthy()
  })
})
