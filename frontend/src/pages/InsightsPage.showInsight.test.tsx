// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Insight } from '@/api/insights'

/**
 * `showInsight` — the insights feed's one page action, and the production
 * caller of the whole page-action path.
 *
 * Driven through `runPageAction`, the registry the widget calls when the agent
 * invokes `page_showInsight`, so what is tested is the action as the agent
 * reaches it — not a function nobody registered.
 */

const rows: Insight[] = [
  { id: 61, content: '[stale] a', project_slug: 'p', project_name: 'P', source: 's', created_at: '2026-09-01T00:00:00Z' },
  { id: 60, content: '[stale] b', project_slug: 'p', project_name: 'P', source: 's', created_at: '2026-09-02T00:00:00Z' },
] as unknown as Insight[]

vi.mock('@/api/insights', async (orig) => ({
  ...(await orig<typeof import('@/api/insights')>()),
  insightsApi: { list: vi.fn(async () => rows), dismiss: vi.fn() },
}))

const { InsightsPage } = await import('./InsightsPage')
const { currentSpecs, runPageAction } = await import('@/widget/pageActions')

beforeEach(() => {
  // jsdom implements no layout, so scrollIntoView does not exist until given.
  Element.prototype.scrollIntoView = vi.fn()
})
afterEach(() => cleanup())

async function renderFeed() {
  render(
    <MemoryRouter initialEntries={['/insights']}>
      <InsightsPage />
    </MemoryRouter>,
  )
  await screen.findByText('a')
}

describe('showInsight', () => {
  it('is offered to the agent while the feed is open', async () => {
    await renderFeed()

    expect(currentSpecs().map((s) => s.name)).toContain('showInsight')
  })

  it('scrolls to the row and highlights it', async () => {
    await renderFeed()

    const result = await act(() => runPageAction('showInsight', { id: 60 }))

    const row = document.querySelector('[data-insight-id="60"]')!
    expect(row.scrollIntoView).toHaveBeenCalled()
    expect(row.className).toContain('ring-primary')
    expect(result).toEqual({ shown: 60 })
    // And only that one.
    expect(document.querySelector('[data-insight-id="61"]')!.className).not.toContain('ring-primary')
  })

  it('refuses, in words, for a row that is not on screen', async () => {
    // Throwing is how a page action refuses: a falsy return would read to the
    // agent as success. The message says WHY, because "a filter hides it" is
    // something the agent should tell the user rather than work around.
    await renderFeed()

    await expect(runPageAction('showInsight', { id: 9999 })).rejects.toThrow(/not on screen/)
  })

  it('is withdrawn when the feed unmounts', async () => {
    // Leaving the page must not leave the agent able to call into it.
    await renderFeed()
    cleanup()

    expect(currentSpecs().map((s) => s.name)).not.toContain('showInsight')
  })
})
