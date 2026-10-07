// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ThemeProvider } from '@/theme/ThemeProvider'
import { ReviewPage, groupScenesByCut } from './ReviewPage'
import type { ReviewDetail } from '../api/reviews'

/**
 * A concept_change review is the first canopy-web page many people ever see —
 * often from an emailed link. The first-use hazards it had (canopy-web#1266–#1271):
 *
 * - A MEMBER fixing one word had a pre-selected "Approve", so the only enabled
 *   button was "Submit — approve & build", and no way to keep an edit without
 *   deciding.
 * - A GUEST was told "you're not approving anything" under a heading asking them
 *   to approve the story, beside build scaffolding (scores, delete, build order).
 * - Guest mode meant "logged out", so a signed-in non-editor got the approve UI.
 * - The demo ran every cut together as one paragraph, under a run id for a name.
 */

const auth = vi.hoisted(() => ({ status: 'authenticated' as string }))
vi.mock('@/auth/AuthProvider', () => ({ useAuth: () => auth }))

const review = vi.hoisted(() => ({ current: null as unknown }))
const suggest = vi.hoisted(() => vi.fn(async () => ({ ok: true, suggestion_count: 1 })))
vi.mock('../api/reviews', async (orig) => ({
  ...(await orig<object>()),
  getReview: vi.fn(async () => review.current),
  suggestReview: suggest,
}))

const detail = (over: Partial<ReviewDetail> = {}): ReviewDetail =>
  ({
    id: 'r1',
    run_id: 'chlorine-dispenser-walkthroughs-2026-10-07-002',
    title: 'Chlorine Dispenser Walkthroughs',
    narrative_slug: null,
    gate: 'concept_change',
    status: 'pending',
    visibility: 'link',
    response_json: null,
    suggestions: [],
    share_token: null,
    is_owner: false,
    can_decide: false,
    created_at: '2026-10-07T00:00:00Z',
    resolved_at: null,
    request_json: {
      schema_version: 3,
      run_id: 'chlorine-dispenser-walkthroughs-2026-10-07-002',
      gate: 'concept_change',
      autonomous_audit: [],
      narrative: 'Register waterpoints. Correct the ward. Decide where dispensers go.',
      narration: [
        { scene: 1, id: 's1', title: 'Cut 1 · Register — registrations land', text: 'Register waterpoints.' },
        { scene: 2, id: 's2', title: 'Cut 1 · Register — ward corrected', text: 'Correct the ward.' },
        { scene: 3, id: 's3', title: 'Cut 2 · Decide — red flags', text: 'Decide where dispensers go.' },
      ],
      decisions: [
        {
          id: 'narrative-verdict',
          prompt: 'Approve this narrative as the build plan, or send it back to re-draft?',
          options: ['approve', 'redraft'],
          recommended: 'approve',
          class: 'concept_change',
        },
      ],
    },
    ...over,
  }) as unknown as ReviewDetail

function renderAt(url: string) {
  window.history.pushState({}, '', url)
  return render(
    <MemoryRouter initialEntries={[url]}>
      <ThemeProvider>
        <Routes>
          <Route path="/review/:id/" element={<ReviewPage />} />
        </Routes>
      </ThemeProvider>
    </MemoryRouter>,
  )
}

const isDisabled = (el: HTMLElement) => (el as HTMLButtonElement).disabled

beforeEach(() => {
  review.current = detail()
  suggest.mockClear()
})
afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/')
})

describe('ReviewPage — a member who can decide', () => {
  beforeEach(() => {
    auth.status = 'authenticated'
    review.current = detail({ can_decide: true })
  })

  it('does not pre-select approve, so nothing commits on the first click', async () => {
    renderAt('/review/r1/')
    const submit = await screen.findByRole('button', { name: /choose approve or re-draft/i })
    expect(isDisabled(submit)).toBe(true)
    expect(screen.queryByRole('button', { name: /submit — approve & build/i })).toBeNull()
  })

  it('enables approve & build only once the member chooses it', async () => {
    renderAt('/review/r1/')
    fireEvent.click(await screen.findByRole('button', { name: /approve & continue/i }))
    expect(isDisabled(screen.getByRole('button', { name: /submit — approve & build/i }))).toBe(false)
  })

  it('can save a wording edit without deciding (#1266)', async () => {
    renderAt('/review/r1/')
    const save = await screen.findByRole('button', { name: /save edits without deciding/i })
    expect(isDisabled(save)).toBe(true) // nothing to save yet
    fireEvent.change(screen.getAllByPlaceholderText(/the beat the viewer watches/i)[0], {
      target: { value: 'Register every waterpoint.' },
    })
    fireEvent.click(save)
    await waitFor(() => expect(suggest).toHaveBeenCalledTimes(1))
    const [, , token] = suggest.mock.calls[0] as unknown as [string, unknown, string | null]
    expect(token).toBeNull() // a session save, not the share link
    expect(await screen.findByText(/saved as a suggestion — nothing was approved/i)).toBeTruthy()
  })

  it('still sees the build plan it is approving', async () => {
    renderAt('/review/r1/')
    expect(await screen.findAllByRole('button', { name: /delete scene/i })).toHaveLength(3)
  })
})

describe('ReviewPage — a guest with the share link', () => {
  beforeEach(() => {
    auth.status = 'anonymous'
  })

  it('asks for suggestions, not approval, and offers no approve control', async () => {
    renderAt('/review/r1/?t=tok')
    expect(await screen.findByRole('heading', { level: 1, name: /suggest edits/i })).toBeTruthy()
    expect(screen.queryByText(/approve the story before we build it/i)).toBeNull()
    expect(screen.queryByRole('button', { name: /approve & continue/i })).toBeNull()
    expect(screen.queryByText(/then approve or send back/i)).toBeNull()
    expect(isDisabled(screen.getByRole('button', { name: /send suggestions/i }))).toBe(false)
  })

  it('shows none of the build scaffolding (#1267)', async () => {
    renderAt('/review/r1/?t=tok')
    await screen.findByRole('heading', { level: 1, name: /suggest edits/i })
    expect(screen.queryByRole('button', { name: /delete scene/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /add scene/i })).toBeNull()
    expect(screen.queryByText(/build sequence/i)).toBeNull()
    expect(screen.queryByText(/actionability/i)).toBeNull()
    expect(screen.queryByText(/existing feature|new feature/i)).toBeNull()
    expect(screen.queryByText(/if you approve/i)).toBeNull()
  })

  it('sends suggestions with the share token', async () => {
    renderAt('/review/r1/?t=tok')
    fireEvent.click(await screen.findByRole('button', { name: /send suggestions/i }))
    await waitFor(() => expect(suggest).toHaveBeenCalledTimes(1))
    expect((suggest.mock.calls[0] as unknown as unknown[])[2]).toBe('tok')
  })
})

describe('ReviewPage — signed in, but not allowed to decide (#1268)', () => {
  it('gets the suggest-only page on the guest link, not the approve UI', async () => {
    auth.status = 'authenticated'
    review.current = detail({ can_decide: false })
    renderAt('/review/r1/?t=tok')
    expect(await screen.findByRole('heading', { level: 1, name: /suggest edits/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /submit — approve & build/i })).toBeNull()
    expect(screen.getByRole('button', { name: /send suggestions/i })).toBeTruthy()
  })
})

describe('ReviewPage — the demo and its name (#1270, #1271)', () => {
  it('names the narrative, not the run id', async () => {
    auth.status = 'anonymous'
    renderAt('/review/r1/?t=tok')
    expect(await screen.findByText('Chlorine Dispenser Walkthroughs')).toBeTruthy()
    expect(screen.queryByText('chlorine-dispenser-walkthroughs-2026-10-07-002')).toBeNull()
  })

  it('renders one labelled paragraph per cut', async () => {
    auth.status = 'anonymous'
    renderAt('/review/r1/?t=tok')
    expect(await screen.findByText('Cut 1 · Register')).toBeTruthy()
    expect(screen.getByText('Cut 2 · Decide')).toBeTruthy()
  })
})

describe('groupScenesByCut', () => {
  it('groups consecutive scenes by the cut named before the em dash', () => {
    const g = groupScenesByCut([
      { title: 'Cut 1 · Register — a' },
      { title: 'Cut 1 · Register — b' },
      { title: 'Cut 2 · Decide — c' },
    ])
    expect(g.map((x) => [x.cut, x.scenes.length])).toEqual([
      ['Cut 1 · Register', 2],
      ['Cut 2 · Decide', 1],
    ])
  })

  it('keeps a narrative without cuts as one paragraph', () => {
    const g = groupScenesByCut([{ title: 'Opening' }, { title: 'Payoff' }])
    expect(g).toHaveLength(1)
    expect(g[0].cut).toBeNull()
  })
})
