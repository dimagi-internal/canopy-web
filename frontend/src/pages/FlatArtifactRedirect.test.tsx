// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

const getWalkthrough = vi.fn()
const getReview = vi.fn()
const getShared = vi.fn()
vi.mock('../api/walkthroughs', () => ({ getWalkthrough }))
vi.mock('../api/reviews', () => ({ getReview }))
vi.mock('../api/sessions', () => ({ getShared }))

const { FlatArtifactRedirect } = await import('./FlatArtifactRedirect')

function Where() {
  const { pathname, search, hash } = useLocation()
  return <p>at {pathname + search + hash}</p>
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route
          path="/walkthrough/:id"
          element={<FlatArtifactRedirect kind="walkthrough"><p>page in place</p></FlatArtifactRedirect>}
        />
        <Route
          path="/review/:id"
          element={<FlatArtifactRedirect kind="review"><p>page in place</p></FlatArtifactRedirect>}
        />
        <Route
          path="/share/:token"
          element={<FlatArtifactRedirect kind="share"><p>page in place</p></FlatArtifactRedirect>}
        />
        <Route path="/w/:workspace/*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('an old flat link moves to the page under its workspace', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('keeps the share token and the timestamp across the move', async () => {
    getWalkthrough.mockResolvedValue({ workspace: 'connect' })
    renderAt('/walkthrough/abc?t=tok#t=12')
    expect(await screen.findByText('at /w/connect/walkthrough/abc?t=tok#t=12')).toBeTruthy()
    expect(getWalkthrough).toHaveBeenCalledWith('abc', 'tok')
  })

  it('moves a review and a shared transcript too', async () => {
    getReview.mockResolvedValue({ workspace: 'connect' })
    renderAt('/review/r1')
    expect(await screen.findByText('at /w/connect/review/r1')).toBeTruthy()
    cleanup()
    getShared.mockResolvedValue({ workspace: 'dimagi' })
    renderAt('/share/s1')
    expect(await screen.findByText('at /w/dimagi/share/s1')).toBeTruthy()
  })

  it('renders the page in place when the workspace cannot be resolved', async () => {
    // Not readable / not found: the page says what it always said here.
    getWalkthrough.mockRejectedValue(new Error('404'))
    renderAt('/walkthrough/gone')
    expect(await screen.findByText('page in place')).toBeTruthy()
    cleanup()
    // An unhomed row has no workspace to move to.
    getShared.mockResolvedValue({ workspace: null })
    renderAt('/share/s1')
    expect(await screen.findByText('page in place')).toBeTruthy()
  })
})
