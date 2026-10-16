// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { ReviewPage } from './ReviewPage'

/**
 * The review page is retired: a narrative review forwards to its storyboard
 * (the server 302s a fresh load; an in-app navigation lands here and asks the
 * destination API). A findings review keeps its own page.
 */

vi.mock('@/auth/AuthProvider', () => ({ useAuth: () => ({ status: 'anonymous' }) }))
const dest = vi.hoisted(() => ({ url: null as string | null }))
vi.mock('../api/reviews', async (orig) => ({
  ...(await orig<object>()),
  getReviewDestination: vi.fn(async () => ({ url: dest.url })),
  getReview: vi.fn(async () => ({
    id: 'r1', run_id: 'x', gate: 'product_findings', status: 'pending', visibility: 'link',
    narrative_slug: null, request_json: { gate: 'product_findings', clusters: [] }, response_json: null,
    resolved_at: null,
  })),
}))
vi.mock('../components/reviews/ProductFindingsReview', () => ({
  ProductFindingsReview: () => <p>findings review</p>,
}))

function Where() {
  const loc = useLocation()
  return <p data-testid="where">{loc.pathname + loc.search + loc.hash}</p>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/w/:workspace/review/:id/" element={<ReviewPage />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(cleanup)

describe('ReviewPage', () => {
  it('forwards a narrative review to its storyboard entry', async () => {
    dest.url = '/w/connect/storyboard/supply-phones?t=board#entry-supply-phones'
    renderAt('/w/connect/review/r1/')
    await waitFor(() =>
      expect(screen.getByTestId('where').textContent).toBe(
        '/w/connect/storyboard/supply-phones?t=board#entry-supply-phones',
      ),
    )
  })

  it('keeps the findings review page', async () => {
    dest.url = null
    renderAt('/w/connect/review/r1/')
    expect(await screen.findByText('findings review')).toBeTruthy()
  })
})
