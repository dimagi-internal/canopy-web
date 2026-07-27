// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import NarrativeReviewPage from './NarrativeReviewPage'

/**
 * The banner tells a reviewer what moved since they last read the narrative.
 * It counted `status !== 'unchanged'` as "changed", so a brand-new scene was
 * reported as a rewording — on `verified-monitoring` that read "6 scenes have
 * changed" when it was 5 reworded and 1 new.
 */
const payload = (over: Record<string, unknown> = {}) => ({
  narrative_slug: 'verified-monitoring',
  title: 'Verified Monitoring',
  story: 'Independent, drillable proof the programme works.',
  version: 17,
  previous_version: 16,
  storyboard_slug: 'rf-surveys',
  storyboard_title: 'Proving a programme works',
  capability: 'suggest',
  previous_narration: [
    { id: 'a', title: 'One', text: 'Old one.' },
    { id: 'b', title: 'Two', text: 'Same.' },
  ],
  narration: [
    { id: 'a', title: 'One', text: 'New one.' },
    { id: 'b', title: 'Two', text: 'Same.' },
    { id: 'c', title: 'Three', text: 'A brand new beat.' },
  ],
  ...over,
})

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/narrative/verified-monitoring?b=rf-surveys&t=tok']}>
      <Routes>
        <Route path="/narrative/:slug" element={<NarrativeReviewPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('NarrativeReviewPage', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => payload() }),
    )
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('counts a new scene as new, not as reworded', async () => {
    renderPage()
    expect(await screen.findByText(/1 scene reworded/)).toBeTruthy()
    expect(screen.getByText(/1 new/)).toBeTruthy()
  })

  it('shows the before/after only on the scene that changed', async () => {
    renderPage()
    await screen.findByText(/1 scene reworded/)
    expect(screen.getByText('Old one.')).toBeTruthy()
    expect(screen.getByText('New one.')).toBeTruthy()
    // The unchanged scene is rendered once, as plain prose — no Was/Now pair.
    expect(screen.getAllByText('Same.')).toHaveLength(1)
  })

  it('says so plainly when nothing moved', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () =>
          payload({
            narration: [{ id: 'a', title: 'One', text: 'Same.' }],
            previous_narration: [{ id: 'a', title: 'One', text: 'Same.' }],
          }),
      }),
    )
    renderPage()
    expect(await screen.findByText(/Nothing has changed since v16/)).toBeTruthy()
  })

  it('names the tab after the narrative and its storyboard', async () => {
    renderPage()
    await screen.findByText(/1 scene reworded/)
    expect(document.title).toContain('Verified Monitoring')
    expect(document.title).toContain('Proving a programme works')
  })

  it('shows a plain message rather than an error dump on 404', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }))
    renderPage()
    expect(await screen.findByText(/isn’t available/)).toBeTruthy()
  })
})
