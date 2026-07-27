// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import StoryboardPage from './StoryboardPage'
import * as api from '@/api/storyboards'

vi.mock('@/api/storyboards', async () => {
  const actual = await vi.importActual<typeof api>('@/api/storyboards')
  return { ...actual, getStoryboard: vi.fn(), leaveFeedback: vi.fn() }
})

const getStoryboard = api.getStoryboard as unknown as ReturnType<typeof vi.fn>

function board(over: Partial<api.Storyboard> = {}): api.Storyboard {
  return {
    slug: 'ecf-supply',
    title: 'What the money bought',
    lede: 'From the first purchase order to the child who recovered.',
    capability: 'read',
    acts: [
      {
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
  beforeEach(() => vi.clearAllMocks())
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
    expect(screen.queryByText('Leave a note')).toBeNull()
  })

  it('offers a composer once the link grants comment', async () => {
    getStoryboard.mockResolvedValue(board({ capability: 'comment' }))
    renderAt()
    expect((await screen.findAllByText('Leave a note')).length).toBeGreaterThan(0)
  })

  it('shows a plain not-available message on 404 rather than an error dump', async () => {
    getStoryboard.mockRejectedValue(new Error('Request failed (404)'))
    renderAt()
    expect(await screen.findByText(/isn’t available/)).toBeTruthy()
  })
})
