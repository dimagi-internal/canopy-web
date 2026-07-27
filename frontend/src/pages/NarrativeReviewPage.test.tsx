// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import NarrativeReviewPage from './NarrativeReviewPage'
import * as api from '@/api/storyboards'

vi.mock('@/api/storyboards', async () => {
  const actual = await vi.importActual<typeof api>('@/api/storyboards')
  return { ...actual, leaveFeedback: vi.fn() }
})

const leaveFeedback = api.leaveFeedback as unknown as ReturnType<typeof vi.fn>

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
    vi.clearAllMocks()
    leaveFeedback.mockResolvedValue({ created: 1 })
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

  /**
   * A cut scene is history — it is not in the narrative being read. Numbering
   * it alongside the others made the scene after it read one higher than it is,
   * and made the footer promise more scenes than the story has.
   */
  describe('a cut scene', () => {
    const withACut = () =>
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({
          ok: true,
          status: 200,
          json: async () =>
            payload({
              previous_narration: [
                { id: 'a', title: 'One', text: 'Kept.' },
                { id: 'gone', title: 'Dropped', text: 'This beat was removed.' },
                { id: 'b', title: 'Two', text: 'Also kept.' },
              ],
              narration: [
                { id: 'a', title: 'One', text: 'Kept.' },
                { id: 'b', title: 'Two', text: 'Also kept.' },
              ],
            }),
        }),
      )

    it('is not numbered among the scenes that remain', async () => {
      withACut()
      const { container } = renderPage()
      await screen.findByText(/1 cut/)
      const numbers = [...container.querySelectorAll('section span.font-mono')].map(
        (n) => n.textContent,
      )
      expect(numbers).toEqual(['01', '02', '—'])
    })

    it('is not counted as a scene of the current narrative', async () => {
      withACut()
      renderPage()
      await screen.findByText(/1 cut/)
      expect(screen.getByText(/2 scenes/)).toBeTruthy()
    })

    it('is not badged as if it were new', async () => {
      withACut()
      renderPage()
      expect(await screen.findByText(/Cut since v16/)).toBeTruthy()
    })

    it('files a note against the version the scene actually exists in', async () => {
      withACut()
      renderPage()
      fireEvent.click(await screen.findByText('Leave a note on the cut scene'))
      fireEvent.change(screen.getByPlaceholderText(/What’s wrong/), {
        target: { value: 'Put this back.' },
      })
      fireEvent.click(screen.getByText('Leave note'))
      await waitFor(() => expect(leaveFeedback).toHaveBeenCalled())
      expect(leaveFeedback.mock.calls[0][1]).toMatchObject({
        anchor_id: 'gone',
        target_version: 16,
      })
    })
  })

  it('shows a plain message rather than an error dump on 404', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }))
    renderPage()
    expect(await screen.findByText(/isn’t available/)).toBeTruthy()
  })
})
