// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ItemCard } from './ItemCard'
import type { ItemOut } from '@/api/items'

afterEach(cleanup)

// The regression was not in the date helper — it was that no card called one.
// Jonathan, 2026-08-12, on a queue of undecided cards: "everything needs to have a
// date displayed on the card, I can't tell if these are recent or just old and I
// should close." So assert it at the level he actually looks at.

function item(over: Partial<ItemOut> = {}): ItemOut {
  return {
    id: 'item-1',
    agent_slug: 'ada',
    idempotency_key: 'k1',
    kind: 'review',
    title: 'A decision that needs making',
    body: '',
    origin: 'api',
    origin_ref: {},
    state: 'open',
    decision: '',
    comment: '',
    decided_by: '',
    decided_by_email: null,
    decided_at: null,
    dispatch: [],
    dispatched_at: null,
    batch_key: 'b1',
    created_at: '2026-08-10T18:00:00Z',
    ...over,
  } as ItemOut
}

describe('ItemCard', () => {
  it('shows the item age on the card', () => {
    render(<ItemCard item={item()} onActed={() => {}} />)
    expect(screen.getByTestId('item-age').textContent).toBeTruthy()
  })

  it('still shows the age when there is no other meta to sit beside it', () => {
    render(<ItemCard item={item({ dispatch: [] })} onActed={() => {}} />)
    expect(screen.getByTestId('item-age')).toBeTruthy()
  })

  it('shows the age on a question, not just a review', () => {
    render(<ItemCard item={item({ kind: 'question' })} onActed={() => {}} />)
    expect(screen.getByTestId('item-age')).toBeTruthy()
  })

  it('keeps the dispatch hint alongside the age', () => {
    render(
      <ItemCard
        item={item({ dispatch: [{ target_agent: 'eva' }] as ItemOut['dispatch'] })}
        onActed={() => {}}
      />,
    )
    expect(screen.getByTestId('item-age')).toBeTruthy()
    expect(document.body.textContent).toContain('dispatches to eva')
  })
})
