// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ThemeProvider } from '@/theme/ThemeProvider'
import NarrativeReviewPage from './NarrativeReviewPage'
import * as api from '@/api/storyboards'

vi.mock('@/api/storyboards', async () => {
  const actual = await vi.importActual<typeof api>('@/api/storyboards')
  return { ...actual, leaveFeedback: vi.fn() }
})

const leaveFeedback = api.leaveFeedback as unknown as ReturnType<typeof vi.fn>

/**
 * The narrative reads CLEAN by default; the comparison with the previous
 * version is a question you ask, via the toggle beside the version pill.
 *
 * Most readers of a shared link are meeting the story for the first time, and
 * struck-through old wording beside new makes a finished narrative look like a
 * work in progress.
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
      <ThemeProvider>
      <Routes>
        <Route path="/narrative/:slug" element={<NarrativeReviewPage />} />
      </Routes>
      </ThemeProvider>
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

  const showChanges = async () => {
    fireEvent.click(await screen.findByRole('button', { name: /Show changes/ }))
  }

  it('reads clean until you ask what changed', async () => {
    renderPage()
    expect(await screen.findByText('New one.')).toBeTruthy()
    expect(screen.queryByText('Old one.')).toBeNull()
    expect(screen.queryByText(/reworded/)).toBeNull()
    expect(screen.queryByText('New')).toBeNull()
  })

  it('counts a new scene as new, not as reworded', async () => {
    renderPage()
    await showChanges()
    expect(await screen.findByText(/1 scene reworded/)).toBeTruthy()
    expect(screen.getByText(/1 new/)).toBeTruthy()
  })

  it('puts the comparison away again', async () => {
    renderPage()
    await showChanges()
    await screen.findByText('Old one.')
    fireEvent.click(screen.getByRole('button', { name: /Hide changes/ }))
    expect(screen.queryByText('Old one.')).toBeNull()
  })

  it('offers no toggle when there is no previous version to compare with', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => payload({ previous_version: null, previous_narration: [] }),
      }),
    )
    renderPage()
    await screen.findByText('New one.')
    expect(screen.queryByRole('button', { name: /Show changes/ })).toBeNull()
  })

  it('shows the before/after only on the scene that changed', async () => {
    renderPage()
    await showChanges()
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
    await showChanges()
    expect(await screen.findByText(/Nothing has changed since v16/)).toBeTruthy()
  })

  it('names the tab after the narrative and its storyboard', async () => {
    renderPage()
    await screen.findByText('New one.')
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

    it('is not on the page at all until you ask what changed', async () => {
      withACut()
      renderPage()
      await screen.findByText('Kept.')
      expect(screen.queryByText('This beat was removed.')).toBeNull()
    })

    it('is not numbered among the scenes that remain', async () => {
      withACut()
      const { container } = renderPage()
      await showChanges()
      await screen.findByText(/1 cut/)
      const numbers = [...container.querySelectorAll('section span.font-mono')].map(
        (n) => n.textContent,
      )
      expect(numbers).toEqual(['01', '02', '—'])
    })

    it('is not counted as a scene of the current narrative', async () => {
      withACut()
      renderPage()
      await showChanges()
      await screen.findByText(/1 cut/)
      expect(screen.getByText(/2 scenes/)).toBeTruthy()
    })

    it('is not badged as if it were new', async () => {
      withACut()
      renderPage()
      await showChanges()
      expect(await screen.findByText(/Cut since v16/)).toBeTruthy()
    })

    it('files a note against the version the scene actually exists in', async () => {
      withACut()
      renderPage()
      await showChanges()
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
