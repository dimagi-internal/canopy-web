// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import StoryboardPage from './StoryboardPage'
import * as api from '@/api/storyboards'

vi.mock('@/api/storyboards', async () => {
  const actual = await vi.importActual<typeof api>('@/api/storyboards')
  return { ...actual, getStoryboard: vi.fn(), leaveFeedback: vi.fn(), getStoryboardNotes: vi.fn() }
})

const getStoryboard = api.getStoryboard as unknown as ReturnType<typeof vi.fn>
const getStoryboardNotes = api.getStoryboardNotes as unknown as ReturnType<typeof vi.fn>

function note(over: Partial<api.Note> = {}): api.Note {
  return {
    id: 1,
    kind: 'comment',
    body: 'Act one runs long.',
    suggested_text: '',
    author_name: 'Sophie',
    channel: 'web',
    state: 'open',
    target_kind: 'storyboard',
    target_ref: 'ecf-supply',
    target_version: null,
    anchor_id: 'act:supply-base',
    created_at: '2026-07-26T10:00:00Z',
    ...over,
  }
}

function board(over: Partial<api.Storyboard> = {}): api.Storyboard {
  return {
    slug: 'ecf-supply',
    title: 'What the money bought',
    lede: 'From the first purchase order to the child who recovered.',
    capability: 'read',
    is_member: false,
    acts: [
      {
        anchor_id: 'act:supply-base',
        title: 'Six weeks to a supply base',
        prose: 'Procurement integrity you can show.',
        entries: [
          {
            narrative_slug: 'procurement',
            title: 'Amina bids, Tomas qualifies her in',
            lede: 'An EOI round opens.',
            version: 4,
            video_url: '/walkthrough/abc/content',
            video_viewer_url: '/walkthrough/abc',
            published: true,
          },
        ],
      },
    ],
    ...over,
  }
}

function renderAt(url = '/storyboard/ecf-supply?t=tok') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/storyboard/:slug" element={<StoryboardPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('StoryboardPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getStoryboardNotes.mockResolvedValue({ items: [] })
  })
  afterEach(cleanup)

  it('renders the arc with acts numbered', async () => {
    getStoryboard.mockResolvedValue(board())
    renderAt()
    expect(await screen.findByText('What the money bought')).toBeTruthy()
    expect(screen.getByText('ACT I')).toBeTruthy()
    expect(screen.getByText('Six weeks to a supply base')).toBeTruthy()
  })

  it('passes the share token through to the API', async () => {
    getStoryboard.mockResolvedValue(board())
    renderAt('/storyboard/ecf-supply?t=secret')
    await waitFor(() => expect(getStoryboard).toHaveBeenCalledWith('ecf-supply', 'secret'))
  })

  it('shows an unbuilt narrative as being built rather than hiding it', async () => {
    getStoryboard.mockResolvedValue(
      board({
        acts: [
          {
            anchor_id: 'act:9',
            title: 'Act',
            prose: '',
            entries: [
              {
                narrative_slug: 'coverage',
                title: 'Coverage against caseload',
                lede: '31,000 children with no carton assigned.',
                version: null,
                video_url: null,
                video_viewer_url: null,
                published: false,
              },
            ],
          },
        ],
      }),
    )
    renderAt()
    expect(await screen.findByText('Being built')).toBeTruthy()
    expect(screen.getByText('Not filmed yet')).toBeTruthy()
    expect(screen.queryByText('Read the scenes →')).toBeNull()
  })

  it('offers no composer on a read-only link', async () => {
    getStoryboard.mockResolvedValue(board({ capability: 'read' }))
    renderAt()
    await screen.findByText('What the money bought')
    expect(screen.queryByText(/Leave a note/)).toBeNull()
  })

  it('names what each composer attaches to', async () => {
    // An act and the demo inside it each get a composer, so they sit side by
    // side — two buttons reading "Leave a note" gave no way to tell them apart.
    getStoryboard.mockResolvedValue(board({ capability: 'comment' }))
    renderAt()
    expect(await screen.findByText('Leave a note on this act')).toBeTruthy()
    expect(screen.getByText('Leave a note on this demo')).toBeTruthy()
  })

  it('says which act an act-level note is about', async () => {
    // Every act note targets the whole board, so with no anchor three notes on
    // three acts arrive indistinguishable — and "act II doesn't follow from act
    // I" is exactly the feedback that means nothing unanchored.
    const leaveFeedback = api.leaveFeedback as unknown as ReturnType<typeof vi.fn>
    leaveFeedback.mockResolvedValue({ created: 1 })
    getStoryboard.mockResolvedValue(board({ capability: 'comment' }))
    renderAt()

    fireEvent.click(await screen.findByText('Leave a note on this act'))
    fireEvent.change(screen.getByPlaceholderText(/What’s wrong/), {
      target: { value: 'Act one runs long.' },
    })
    fireEvent.click(screen.getByText('Leave note'))

    await waitFor(() => expect(leaveFeedback).toHaveBeenCalled())
    expect(leaveFeedback.mock.calls[0][1].anchor_id).toBe('act:supply-base')
  })

  describe('what the reviewer is being asked to do', () => {
    it('is said plainly to someone who opened the link cold', async () => {
      getStoryboard.mockResolvedValue(board({ capability: 'comment' }))
      renderAt()
      expect(await screen.findByText(/Leave a note anywhere something is wrong/)).toBeTruthy()
      expect(screen.getByText(/to nobody else/)).toBeTruthy()
    })

    it('mentions suggested wording only when the link grants it', async () => {
      getStoryboard.mockResolvedValue(board({ capability: 'comment' }))
      renderAt()
      await screen.findByText(/Leave a note anywhere/)
      expect(screen.queryByText(/exact wording you would use/)).toBeNull()

      cleanup()
      getStoryboard.mockResolvedValue(board({ capability: 'suggest' }))
      renderAt()
      expect(await screen.findByText(/exact wording you would use/)).toBeTruthy()
    })

    it('says nothing on a read-only link, which asks for nothing', async () => {
      getStoryboard.mockResolvedValue(board({ capability: 'read' }))
      renderAt()
      await screen.findByText('What the money bought')
      expect(screen.queryByText(/Leave a note anywhere/)).toBeNull()
    })

    it('does not instruct the people who sent it', async () => {
      getStoryboard.mockResolvedValue(board({ capability: 'suggest', is_member: true }))
      renderAt()
      await screen.findByText('What the money bought')
      expect(screen.queryByText(/Leave a note anywhere/)).toBeNull()
    })
  })

  it('shows a plain not-available message on 404 rather than an error dump', async () => {
    getStoryboard.mockRejectedValue(new Error('Request failed (404)'))
    renderAt()
    expect(await screen.findByText(/isn’t available/)).toBeTruthy()
  })

  it('names the tab after the storyboard, not the app', async () => {
    // A shared link gets bookmarked and sits among a dozen other tabs; "Canopy"
    // does not say which one this is.
    getStoryboard.mockResolvedValue(board())
    renderAt()
    await screen.findByText('What the money bought')
    expect(document.title).toContain('What the money bought')
  })

  describe('the notes that came back', () => {
    it('are not fetched at all for someone holding the link', async () => {
      getStoryboard.mockResolvedValue(board({ is_member: false }))
      renderAt()
      await screen.findByText('What the money bought')
      expect(getStoryboardNotes).not.toHaveBeenCalled()
    })

    it('never render a reviewer another reviewer’s words', async () => {
      // The fetch 401s for a non-member anyway; this is the second lock.
      getStoryboard.mockResolvedValue(board({ is_member: false }))
      getStoryboardNotes.mockResolvedValue({ items: [note()] })
      renderAt()
      await screen.findByText('What the money bought')
      expect(screen.queryByText('Act one runs long.')).toBeNull()
    })

    it('appear under the act they were left on', async () => {
      getStoryboard.mockResolvedValue(board({ is_member: true }))
      getStoryboardNotes.mockResolvedValue({ items: [note()] })
      renderAt()
      expect(await screen.findByText('Act one runs long.')).toBeTruthy()
      expect(screen.getByText('Sophie')).toBeTruthy()
    })

    it('appear under the demo they were left on', async () => {
      getStoryboard.mockResolvedValue(board({ is_member: true }))
      getStoryboardNotes.mockResolvedValue({
        items: [
          note({
            id: 2,
            target_kind: 'narrative',
            target_ref: 'procurement',
            target_version: 4,
            anchor_id: 'the-goal',
            body: 'Amina would not say it that way.',
          }),
        ],
      })
      renderAt()
      expect(await screen.findByText('Amina would not say it that way.')).toBeTruthy()
      // The version it was left against, alongside the card's own version chip.
      expect(screen.getAllByText('v4')).toHaveLength(2)
    })

    it('collect board-wide notes rather than dropping them', async () => {
      getStoryboard.mockResolvedValue(board({ is_member: true }))
      getStoryboardNotes.mockResolvedValue({
        items: [note({ id: 3, anchor_id: '', body: 'The whole arc is too long.' })],
      })
      renderAt()
      expect(await screen.findByText('On the arc as a whole')).toBeTruthy()
      expect(screen.getByText('The whole arc is too long.')).toBeTruthy()
    })

    it('show a suggestion’s proposed wording, which lives in another field', async () => {
      getStoryboard.mockResolvedValue(board({ is_member: true }))
      getStoryboardNotes.mockResolvedValue({
        items: [
          note({ id: 4, kind: 'suggestion', body: '', suggested_text: '…a QC re-visit.' }),
        ],
      })
      renderAt()
      expect(await screen.findByText('…a QC re-visit.')).toBeTruthy()
      expect(screen.getByText('Suggested wording')).toBeTruthy()
    })

    it('say nothing at all when none have come back', async () => {
      getStoryboard.mockResolvedValue(board({ is_member: true }))
      renderAt()
      await screen.findByText('What the money bought')
      expect(screen.queryByText(/notes? back/)).toBeNull()
    })
  })

  it('gives each demo video an accessible name', async () => {
    getStoryboard.mockResolvedValue(board())
    const { container } = renderAt()
    await screen.findByText('What the money bought')
    const videos = [...container.querySelectorAll('video')]
    expect(videos.length).toBeGreaterThan(0)
    for (const v of videos) expect(v.getAttribute('aria-label')).toBeTruthy()
  })
})
