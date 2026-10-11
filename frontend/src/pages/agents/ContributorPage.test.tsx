// @vitest-environment jsdom
//
// A contributor page: what someone is trying to achieve, in their own words with
// a link to where they said each thing, and their ideas — built versus open.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const { getContributorPage } = vi.hoisted(() => ({
  getContributorPage: vi.fn<(slug: string, ref: string, person: string) => Promise<unknown>>(),
}))

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getContributorPage,
}))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({ agent: { slug: 'hal', name: 'Hal' }, canEdit: true }),
}))

const { ContributorPage } = await import('./ContributorPage')

const said = 'https://canopy.test/w/connect/chat/abc'

function idea(over: Record<string, unknown>) {
  return {
    ext_id: 'T1', title: 'Idea', status: 'done', stage: 'shipped', next_action: '',
    updated_at: '2026-10-05T00:00:00Z', links: [],
    came_from: [{ person_id: 3, name: 'Jonathan', via: 'contributor_page', quote: 'q',
                  source_url: said, said_at: null }],
    their_words: { person_id: 3, name: 'Jonathan', via: 'contributor_page', quote: 'q',
                   source_url: said, said_at: null },
    ...over,
  }
}

function page(over: Record<string, unknown> = {}) {
  return {
    person: { id: 3, display_name: 'Jonathan Jackson', email: 'jj@dimagi.com' },
    agent: 'hal', project: 'P5', project_name: 'Supply', role: '', participant_since: null,
    summary_state: 'written',
    summary: {
      goal: 'Make what is going on in a procurement visible before recommending anything.',
      quotes: [{ quote: 'the normal case is someone just ignores us', source_url: said,
                 said_at: '2026-09-27T00:00:00Z', where: 'Supply session, Sep 27' }],
      open: [{ text: 'Per-org page config', quote: 'Pages should just be completely dynamic',
               source_url: said, said_at: null }],
      sources: [{ label: 'Two-pager', url: 'https://docs.google.com/document/d/x' }],
      basis: '458 prompts across 22 sessions', written_at: '2026-10-11T00:00:00Z', written_by: 'hal',
    },
    ideas: [
      idea({ ext_id: 'T71', title: "Sophie's RUTF tender", status: 'in_progress', stage: 'started',
             their_words: { person_id: 3, name: 'Jonathan', via: 'contributor_page',
                            quote: 'something sophie can start using', source_url: said, said_at: null } }),
      idea({ ext_id: 'T66', title: 'Project-history skill' }),
    ],
    stages: { proposed: 0, started: 1, shipped: 1, declined: 0 },
    may_write: false, why_not: '',
    ...over,
  }
}

function show() {
  return render(
    <MemoryRouter initialEntries={['/w/connect/agents/hal/projects/P5/people/3']}>
      <Routes>
        <Route path="/w/:workspace/agents/:slug/projects/:ref/people/:person" element={<ContributorPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ContributorPage', () => {
  it('leads with their goal and their own words, each linked to where they said it', async () => {
    getContributorPage.mockResolvedValue(page())
    show()
    expect((await screen.findByTestId('contributor-goal')).textContent).toContain('visible before')
    const link = screen.getByRole('link', { name: 'Supply session, Sep 27' })
    expect(link.getAttribute('href')).toBe(said)
    expect(screen.getByText(/the normal case is someone just ignores us/)).toBeTruthy()
    expect(screen.getByTestId('contributor-tally').textContent).toContain('1 shipped')
    expect(getContributorPage).toHaveBeenCalledWith('hal', 'P5', '3')
  })

  it('splits their ideas into built and still open, with their words on each', async () => {
    getContributorPage.mockResolvedValue(page())
    show()
    expect(await screen.findByText('Built so far (1)')).toBeTruthy()
    expect(screen.getByText('In progress (1)')).toBeTruthy()
    const tender = screen.getByTestId('idea-T71')
    expect(tender.textContent).toContain('something sophie can start using')
    expect(within(tender).getByLabelText('Stage: Started')).toBeTruthy()
    expect(screen.getByTestId('contributor-open').textContent).toContain('Per-org page config')
  })

  it('says why there is no page when an agent may not write it', async () => {
    getContributorPage.mockResolvedValue(
      page({ summary_state: 'not_written', summary: null, ideas: [],
             stages: { proposed: 0, started: 0, shipped: 0, declined: 0 },
             why_not: 'this person has not let agents learn about them' }),
    )
    show()
    expect((await screen.findByTestId('contributor-unwritten')).textContent).toContain(
      'not let agents learn about them',
    )
  })
})
