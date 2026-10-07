// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ThemeProvider } from '@/theme/ThemeProvider'
import { ReviewPage } from './ReviewPage'
import type { ReviewDetail } from '../api/reviews'

/**
 * A concept_change review is the first canopy-web page many people ever see —
 * often from an emailed link. Two first-use hazards (canopy-web#1266, #1267):
 *
 * - A MEMBER fixing one word had a pre-selected "Approve", so the only enabled
 *   button was "Submit — approve & build": one click locked the build plan.
 * - A GUEST was told "you're not approving anything" under a heading asking them
 *   to approve the story, above a green, pre-selected "Approve & continue".
 */

const auth = vi.hoisted(() => ({ status: 'authenticated' as string }))
vi.mock('@/auth/AuthProvider', () => ({ useAuth: () => auth }))

const review = vi.hoisted(() => ({ current: null as unknown }))
vi.mock('../api/reviews', async (orig) => ({
  ...(await orig<object>()),
  getReview: vi.fn(async () => review.current),
}))

const detail = (): ReviewDetail =>
  ({
    id: 'r1',
    run_id: 'chlorine-dispenser-walkthroughs-2026-10-07-002',
    narrative_slug: null,
    gate: 'concept_change',
    status: 'pending',
    visibility: 'link',
    response_json: null,
    suggestions: [],
    share_token: null,
    is_owner: false,
    created_at: '2026-10-07T00:00:00Z',
    resolved_at: null,
    request_json: {
      schema_version: 3,
      run_id: 'chlorine-dispenser-walkthroughs-2026-10-07-002',
      gate: 'concept_change',
      autonomous_audit: [],
      narrative: 'Register waterpoints. Decide where dispensers go.',
      narration: [
        { scene: 1, id: 's1', title: 'Cut 1 · Register', text: 'Register waterpoints.' },
        { scene: 2, id: 's2', title: 'Cut 2 · Decide', text: 'Decide where dispensers go.' },
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

beforeEach(() => {
  review.current = detail()
})
afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/')
})

describe('ReviewPage — a member fixing one word', () => {
  beforeEach(() => {
    auth.status = 'authenticated'
  })

  it('does not pre-select approve, so nothing commits on the first click', async () => {
    renderAt('/review/r1/')
    const submit = await screen.findByRole('button', { name: /choose approve or re-draft/i })
    expect((submit as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByRole('button', { name: /submit — approve & build/i })).toBeNull()
  })

  it('enables approve & build only once the member chooses it', async () => {
    renderAt('/review/r1/')
    fireEvent.click(await screen.findByRole('button', { name: /approve & continue/i }))
    const submit = screen.getByRole('button', { name: /submit — approve & build/i })
    expect((submit as HTMLButtonElement).disabled).toBe(false)
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
    const send = screen.getByRole('button', { name: /send suggestions/i })
    expect((send as HTMLButtonElement).disabled).toBe(false)
  })
})
