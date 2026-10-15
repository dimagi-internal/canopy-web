// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ThemeProvider } from '@/theme/ThemeProvider'
import StoryboardPage from './StoryboardPage'
import * as api from '@/api/storyboards'
import { runningOrder, totalOf } from '@/components/storyboard/DemoBrief'

/**
 * A storyboard as the ONE link for a set of recorded demos (Parts 1–3 of the
 * Connect Supply demos). Before this, everything below lived in a companion
 * Google Doc: who it is for, the point to land, the synthetic-data disclosure,
 * the running order and the 3-minute version, each cut's length / words / what
 * is on screen / why it matters / where the data comes from, a way to comment
 * on a cut as a whole, and where the script and the data disagree.
 */

vi.mock('@/api/storyboards', async () => {
  const actual = await vi.importActual<typeof api>('@/api/storyboards')
  return { ...actual, getStoryboard: vi.fn(), leaveFeedback: vi.fn(), getStoryboardNotes: vi.fn() }
})
const getStoryboard = api.getStoryboard as unknown as ReturnType<typeof vi.fn>
const getStoryboardNotes = api.getStoryboardNotes as unknown as ReturnType<typeof vi.fn>
const leaveFeedback = api.leaveFeedback as unknown as ReturnType<typeof vi.fn>

function cut(over: Partial<api.EntryCut> = {}): api.EntryCut {
  return {
    cut_id: 'procurements',
    anchor_id: 'cut:procurements',
    title: 'Every procurement in one place',
    recorded: true,
    video_url: '/w/connect/walkthrough/w1/content?t=pub',
    video_viewer_url: '/w/connect/walkthrough/w1?t=pub',
    poster_url: null,
    duration_sec: 21,
    word_count: 62,
    scenes: [{ id: 's1', title: 'Every procurement', text: 'Each line is a tender or an order.' }],
    screen_notes: 'Overview page; cursor rests on the tender row.',
    audience_note: 'For Edesia: their order is the one on the road.',
    provenance: 'supply_chain overview, programme 10112',
    optional: false,
    ...over,
  }
}

function decision(over: Partial<api.Decision> = {}): api.Decision {
  return {
    id: 'd', scope: '', question: 'q', plain_question: 'q', options: [], recommended: '', override: '', value: '',
    reasoning: '', override_reasoning: '', source: '', evidence_basis: 'stated', conflict_signals: [],
    status: 'ai-default', revisit_when: '', review_ask: '', needed_by: '', confirm_reason: '', decided_by: '',
    decided_by_verified: false, decided_at: null, confirmed: false, history: [], supersedes: '', superseded_by: '',
    editable: true, ...over,
  }
}

function board(over: Partial<api.Storyboard> = {}): api.Storyboard {
  return {
    slug: 'supply',
    title: 'Connect Supply demos',
    lede: 'Three short demos.',
    capability: 'comment',
    layout: 'review',
    is_member: false,
    brief: {
      audience: 'Edesia and OES',
      purpose: 'One set of screens shows where every sachet is.',
      data_disclosure: 'All on-screen data is synthetic. Source: Labs programme 10112.',
      short_version: { label: '3-minute version', items: [{ narrative_slug: 'rutf', cut_id: 'buying' }] },
    },
    decisions: [
      decision({ id: 'partner-store', plain_question: 'Name the partner store?', review_ask: 'required-before', needed_by: 'external-share' }),
      decision({ id: 'stand-in', plain_question: 'Programme 10112 stands in for the real programme.', review_ask: '' }),
    ],
    acts: [
      {
        anchor_id: 'act:part-1',
        title: 'Part 1',
        prose: '',
        entries: [
          {
            narrative_slug: 'rutf',
            title: 'RUTF stock at scale',
            lede: 'Every procurement, every store.',
            version: 1,
            video_url: null,
            video_viewer_url: null,
            published: true,
            cuts: [
              cut(),
              cut({ cut_id: 'buying', anchor_id: 'cut:buying', title: 'Buying RUTF', duration_sec: 44,
                    video_url: null, recorded: false, screen_notes: '', audience_note: '', provenance: '' }),
            ],
            open_questions: ['Name the partner store?'],
          },
        ],
      },
    ],
    ...over,
  }
}

function renderAt(url = '/w/connect/storyboard/supply?t=tok') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <ThemeProvider>
        <Routes>
          <Route path="/w/:workspace/storyboard/:slug" element={<StoryboardPage />} />
        </Routes>
      </ThemeProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  getStoryboardNotes.mockResolvedValue({ items: [] })
  leaveFeedback.mockResolvedValue({ created: 1 })
})
afterEach(cleanup)

describe('Storyboard as a demo set', () => {
  it('says who it is for, the point to land, and that the data is synthetic', async () => {
    getStoryboard.mockResolvedValue(board())
    renderAt()
    const about = await screen.findByRole('region', { name: 'About this set' })
    expect(within(about).getByText('Edesia and OES')).toBeTruthy()
    expect(within(about).getByText(/where every sachet is/)).toBeTruthy()
    expect(within(about).getByText(/All on-screen data is synthetic/)).toBeTruthy()
  })

  it('gives the running order with lengths, and the short version', async () => {
    getStoryboard.mockResolvedValue(board())
    renderAt()
    const about = await screen.findByRole('region', { name: 'About this set' })
    expect(about.textContent).toContain('Running order')
    expect(about.textContent).toContain('1:05 in all')
    expect(about.textContent).toContain('3-minute version')
    const links = within(about).getAllByRole('link', { name: /Buying RUTF/ })
    expect(links[0].getAttribute('href')).toBe('#cut-rutf-buying')
  })

  it('shows each cut with its length, words, screen notes, audience note and source', async () => {
    getStoryboard.mockResolvedValue(board())
    renderAt()
    const c = await screen.findByRole('region', { name: 'Every procurement in one place' })
    expect(c.textContent).toContain('0:21 · 62 words')
    expect(c.textContent).toContain('cursor rests on the tender row')
    expect(c.textContent).toContain('For Edesia')
    expect(c.textContent).toContain('programme 10112')
    expect(c.querySelector('video')?.getAttribute('src')).toContain('/w/connect/walkthrough/w1/content?t=pub')
    const missing = screen.getByRole('region', { name: 'Buying RUTF' })
    expect(missing.querySelector('video')).toBeNull()
    expect(missing.textContent).toContain('Not recorded yet')
  })

  it('links each cut to the live product screen it films, in a new tab', async () => {
    const b = board()
    b.acts[0].entries[0].cuts![0] = {
      ...b.acts[0].entries[0].cuts![0],
      app_links: [{ label: 'Supply overview', url: 'https://labs.connect.dimagi.com/supply/overview/?program_id=10112' }],
    }
    getStoryboard.mockResolvedValue(b)
    renderAt()
    const c = await screen.findByRole('region', { name: 'Every procurement in one place' })
    const link = within(c).getByRole('link', { name: /Open Supply overview in Labs/ })
    expect(link.getAttribute('href')).toBe('https://labs.connect.dimagi.com/supply/overview/?program_id=10112')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toContain('noopener')
    // A cut with no product screen recorded offers no dead link.
    const missing = screen.getByRole('region', { name: 'Buying RUTF' })
    expect(within(missing).queryByText(/Try it live/)).toBeNull()
  })

  it('takes a comment on a cut as a whole, anchored to that cut', async () => {
    getStoryboard.mockResolvedValue(board())
    renderAt()
    const c = await screen.findByRole('region', { name: 'Every procurement in one place' })
    fireEvent.click(within(c).getByRole('button', { name: 'Comment on this cut' }))
    fireEvent.change(within(c).getByPlaceholderText(/what’s wrong/i), {
      target: { value: 'Too fast on the tender row.' },
    })
    fireEvent.click(within(c).getByRole('button', { name: 'Leave note' }))
    await waitFor(() => expect(leaveFeedback).toHaveBeenCalledTimes(1))
    const payload = leaveFeedback.mock.calls[0][1] as api.LeaveFeedbackIn
    expect(payload).toMatchObject({ narrative_slug: 'rutf', target_version: 1, anchor_id: 'cut:procurements' })
  })

  it('shows a member the notes left on a cut under that cut', async () => {
    getStoryboard.mockResolvedValue(board({ is_member: true }))
    getStoryboardNotes.mockResolvedValue({
      items: [{
        id: 9, kind: 'comment', body: 'Show the map first.', suggested_text: '', author_name: 'Lily',
        channel: 'web', state: 'open', target_kind: 'narrative', target_ref: 'rutf', target_version: 1,
        anchor_id: 'cut:procurements', created_at: '2026-10-15T00:00:00Z',
      }],
    })
    renderAt('/w/connect/storyboard/supply')
    const c = await screen.findByRole('region', { name: 'Every procurement in one place' })
    await waitFor(() => expect(c.textContent).toContain('Show the map first.'))
    expect(screen.getAllByText('Show the map first.')).toHaveLength(1)
  })

  it('ends with the decision log, asks first', async () => {
    getStoryboard.mockResolvedValue(board())
    renderAt()
    const log = await screen.findByRole('region', { name: 'Decisions' })
    expect(log.textContent).toContain('Answer before sharing')
    expect(log.textContent).toContain('Name the partner store?')
    expect(log.textContent).toContain('What the agent assumed')
    expect(log.textContent).toContain('Programme 10112 stands in')
  })

  it('renders a board without a brief or cuts exactly as before', async () => {
    getStoryboard.mockResolvedValue(board({
      brief: undefined,
      decisions: [],
      acts: [{ anchor_id: 'act:a', title: 'A', prose: '', entries: [{
        narrative_slug: 'x', title: 'X', lede: '', version: 1, video_url: '/v', video_viewer_url: '/v', published: true,
      }] }],
    }))
    renderAt()
    await screen.findByText('X')
    expect(screen.queryByRole('region', { name: 'About this set' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'Decisions' })).toBeNull()
  })

  it('on a reel shows the cuts and nothing to answer', async () => {
    getStoryboard.mockResolvedValue(board({ layout: 'reel' }))
    renderAt()
    const c = await screen.findByRole('region', { name: 'Every procurement in one place' })
    expect(within(c).queryByRole('button', { name: 'Comment on this cut' })).toBeNull()
    expect(c.textContent).not.toContain('cursor rests')
    // The disclosure is not review furniture.
    expect(screen.getByText(/All on-screen data is synthetic/)).toBeTruthy()
  })
})

describe('runningOrder', () => {
  it('lists every cut in arc order and totals the known lengths', () => {
    const order = runningOrder(board())
    expect(order.map((o) => o.title)).toEqual(['Every procurement in one place', 'Buying RUTF'])
    expect(totalOf(order)).toEqual({ sec: 65, partial: false })
  })
})
