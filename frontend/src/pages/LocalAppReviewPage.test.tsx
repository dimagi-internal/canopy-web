// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, matchRoutes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const getSubmission = vi.fn()
const revoke = vi.fn()
vi.mock('@/api/localApps', async (original) => ({
  ...(await original<typeof import('@/api/localApps')>()),
  getLocalAppSubmission: (id: number) => getSubmission(id),
  revokeLocalAppVersion: (id: number, reason: string) => revoke(id, reason),
}))

import { ApiError } from '@/api/problem'
import { diffLineKind, LocalAppDiff } from '@/components/localApps/LocalAppDiff'
import { routeTable } from '@/router'
import LocalAppReviewPage from './LocalAppReviewPage'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

const SUB = {
  id: 9, key: 'meeting-assistant', version: '0.2.0', sha256: 'a'.repeat(64), status: 'pending', approval_id: 41,
  review_url: 'https://x/w/dimagi/apps/local/review/9', name: 'Meeting assistant', size: 2048, note: 'Fixes the batching.',
  submitted_by: 'ace@dimagi-ai.com', submitted_at: '2026-11-03T10:00:00Z',
  manifest: { notice: 'It leaves your Mac.', disclosure: { reads: ['the transcript'], sends: ['text to a session'] } },
  facts: { files: [{ path: 'app.py', size: 2048 }], network: {}, process: { 'os.system': ['app.py'] }, risky: {}, unparsed: [] },
  prior_version: '0.1.0', trust_changes: ['Now also reads your calendar'],
  diff: [{ path: 'app.py', status: 'changed', lines: ['--- app.py', '+++ app.py', '@@ -1 +1 @@', '-old', '+new'], truncated: false }],
  revoked_reason: '', can_revoke: false,
}

function open(path = '/w/dimagi/apps/local/review/9') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes><Route path="/w/:workspace/apps/local/review/:id" element={<LocalAppReviewPage />} /></Routes>
    </MemoryRouter>,
  )
}

describe('LocalAppDiff', () => {
  it('colours lines by what they are, and reads code that looks like a header as code', () => {
    expect(diffLineKind('--- app.py', 0)).toBe('header')
    expect(diffLineKind('+++ app.py', 1)).toBe('header')
    expect(diffLineKind('@@ -1 +1 @@', 2)).toBe('hunk')
    expect(diffLineKind('+new', 3)).toBe('added')
    expect(diffLineKind('-old', 4)).toBe('removed')
    expect(diffLineKind(' same', 5)).toBe('context')
    // A removed `-- comment` and an added `++x`, well after the file header.
    expect(diffLineKind('--- comment', 6)).toBe('removed')
    expect(diffLineKind('+++x', 7)).toBe('added')
  })

  it('tints with semantic tokens and sets the lines in monospace', () => {
    const { container } = render(<LocalAppDiff files={[{ path: 'a.py', status: 'changed', truncated: false, lines: ['--- a.py', '+++ a.py', '-old', '+new'] }]} />)
    const added = container.querySelector('[data-line="added"]')
    expect(added?.className).toContain('text-success')
    expect(container.querySelector('[data-line="removed"]')?.className).toContain('text-destructive')
    expect(container.querySelector('pre')?.className).toContain('font-mono')
  })

  it('opens a short file and closes a long one', () => {
    const long = Array.from({ length: 201 }, (_, i) => `+line ${i}`)
    render(<LocalAppDiff files={[
      { path: 'short.py', status: 'added', truncated: false, lines: ['+one'] },
      { path: 'long.py', status: 'added', truncated: false, lines: long },
    ]} />)
    const [short, big] = screen.getAllByTestId('local-app-file-diff') as HTMLDetailsElement[]
    expect(short.open).toBe(true)
    expect(big.open).toBe(false)
    expect(big.textContent).toContain('closed because it is long')
  })

  it('says when a file is cut short, and when it was not reached at all', () => {
    render(<LocalAppDiff files={[
      { path: 'cut.py', status: 'added', truncated: true, lines: ['+one'] },
      { path: 'late.py', status: 'added', truncated: true, lines: [] },
      { path: 'empty.py', status: 'added', truncated: false, lines: [] },
    ]} />)
    const notes = screen.getAllByTestId('local-app-diff-truncated')
    expect(notes[0].textContent).toContain('This diff stops here')
    expect(notes[1].textContent).toContain('before this file')
    expect(screen.getByText('An empty file.')).toBeTruthy()
  })
})

describe('LocalAppReviewPage', () => {
  it('shows the submission, its facts highlighted when found, and their note as their words', async () => {
    getSubmission.mockResolvedValue(SUB)
    open()
    expect(await screen.findByText('Meeting assistant 0.2.0')).toBeTruthy()
    expect(getSubmission).toHaveBeenCalledWith(9)
    expect(screen.getByTestId('local-app-review-status').textContent).toBe('Waiting for approval')
    expect(screen.getByText('a'.repeat(64))).toBeTruthy()
    const note = screen.getByTestId('local-app-review-note')
    expect(note.textContent).toContain('ace@dimagi-ai.com own words')
    expect(note.textContent).toContain('Fixes the batching.')
    const facts = screen.getAllByTestId('local-app-fact')
    expect(facts.map((f) => f.getAttribute('data-found'))).toEqual([null, 'true', null])
    expect(facts[1].textContent).toContain('os.system')
    expect(screen.getByTestId('local-app-review-trust').textContent).toContain('Now also reads your calendar')
    expect(screen.getByText('-old')).toBeTruthy()
  })

  it('approves on the approval page: a link to it, and no button here', async () => {
    getSubmission.mockResolvedValue(SUB)
    open()
    const link = await screen.findByRole('link', { name: 'Approve or decline on the approval page' })
    expect(link.getAttribute('href')).toBe('/w/dimagi/approvals/41')
    expect(screen.queryByRole('button', { name: /approve/i })).toBeNull()
  })

  it('says plainly when part of the change is not shown', async () => {
    getSubmission.mockResolvedValue({ ...SUB, diff: [{ ...SUB.diff[0], truncated: true }] })
    open()
    expect((await screen.findByTestId('local-app-review-cut')).textContent).toContain('1 file is cut short')
  })

  it.each([
    ['a submission the API says is not there', '/w/dimagi/apps/local/review/9'],
    ['an id that is not a number', '/w/dimagi/apps/local/review/nope'],
  ])('shows not found, or not yours, for %s', async (_, path) => {
    getSubmission.mockRejectedValue(new ApiError(404, 'no such submission'))
    open(path)
    expect((await screen.findByTestId('local-app-review-missing')).textContent).toBe('Not found, or not yours.')
  })

  it('offers no revoke without can_revoke', async () => {
    getSubmission.mockResolvedValue(SUB)
    open()
    await screen.findByText('Meeting assistant 0.2.0')
    expect(screen.queryByRole('form', { name: 'Revoke' })).toBeNull()
  })

  it('needs a reason to revoke, sends it after a yes, and shows the result', async () => {
    getSubmission.mockResolvedValueOnce({ ...SUB, status: 'released', can_revoke: true })
    getSubmission.mockResolvedValue({ ...SUB, status: 'revoked', can_revoke: false, revoked_reason: 'it leaks' })
    revoke.mockResolvedValue({})
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    open()
    const button = await screen.findByRole('button', { name: 'Revoke' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Why'), { target: { value: ' it leaks ' } })
    fireEvent.click(button)
    await waitFor(() => expect(revoke).toHaveBeenCalledWith(9, 'it leaks'))
    expect((await screen.findByTestId('local-app-review-revoked')).textContent).toContain('it leaks')
    expect(screen.queryByRole('form', { name: 'Revoke' })).toBeNull()
  })
})

describe('the routes', () => {
  const leaf = (path: string) => matchRoutes(routeTable, path)?.at(-1)?.route.path

  it('reach the local app pages, not a web app called "local"', () => {
    expect(leaf('/w/dimagi/apps/local/meeting-assistant')).toBe('/w/:workspace/apps/local/:key')
    expect(leaf('/w/dimagi/apps/local/review/9')).toBe('/w/:workspace/apps/local/review/:id')
    expect(leaf('/w/dimagi/apps/hiring')).toBe('/w/:workspace/apps/:appKey/:page?')
  })
})
