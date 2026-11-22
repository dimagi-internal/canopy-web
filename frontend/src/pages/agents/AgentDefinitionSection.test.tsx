// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'

// The rules are the server's (apps/agent_definitions/tests); pinned here is that the page
// offers Publish and Roll back only when the server says the viewer may, and that each
// button calls its route.
vi.mock('@/api/agentDefinitions', () => ({
  getDefinition: vi.fn(),
  listRevisions: vi.fn(),
  getRevision: vi.fn(),
  getRevisionDiff: vi.fn(),
  getRevisionFile: vi.fn(),
  publishRevision: vi.fn(),
  discardRevision: vi.fn(),
  rollbackTo: vi.fn(),
}))

import * as api from '@/api/agentDefinitions'
import { AgentDefinitionSection } from './AgentDefinitionSection'

const m = api as unknown as Record<string, ReturnType<typeof vi.fn>>
const SHA = 'b'.repeat(64)

function rev(number: number, status: string, extra: Record<string, unknown> = {}) {
  return { number, status, root_sha256: SHA, message: `r${number}`, parent: number > 1 ? number - 1 : null,
    author: { email: 'olive@dimagi.com', by_agent: false, turn_id: null }, files: 2, size: 10,
    created_at: '2026-11-21T00:00:00Z', published_at: null, published_by: null, approval_id: null, ...extra }
}

function setup(canPublish: boolean) {
  m.getDefinition.mockResolvedValue({ source: 'stored', live: rev(2, 'published'), draft: rev(3, 'draft'),
    policy: { max_files: 200, max_file_bytes: 1, max_tree_bytes: 1, editable: [], generated: [] },
    can_draft: true, can_publish: canPublish })
  m.listRevisions.mockResolvedValue([rev(3, 'draft'), rev(2, 'published'), rev(1, 'superseded')])
  m.getRevision.mockResolvedValue({ ...rev(3, 'draft'), tree: { 'persona.md': { sha256: SHA, size: 3, executable: false } } })
  m.getRevisionDiff.mockResolvedValue({ revision: 3, against: 2, files: [
    { path: 'persona.md', status: 'modified', diff: '-old\n+new\n', truncated: false }] })
  m.getRevisionFile.mockResolvedValue('new\n')
  m.publishRevision.mockResolvedValue(rev(3, 'published'))
  m.rollbackTo.mockResolvedValue(rev(4, 'published'))
  const agent = { slug: 'scout', name: 'Scout', workspace: 'scouts' }
  return render(
    <MemoryRouter initialEntries={['/w/scouts/agents/scout/definition']}>
      <Routes>
        <Route path="/w/:workspace/agents/:slug" element={<Outlet context={{ agent }} />}>
          <Route path="definition" element={<AgentDefinitionSection />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentDefinitionSection', () => {
  it('shows the draft diff and the files, and publishes for an admin', async () => {
    setup(true)
    await waitFor(() => expect(screen.getByTestId('definition-draft-diff').textContent).toContain('persona.md'))
    fireEvent.click(await screen.findByRole('button', { name: 'persona.md' }))
    await waitFor(() => expect(screen.getByTestId('definition-file-text').textContent).toBe('new\n'))
    fireEvent.click(screen.getByRole('button', { name: 'Publish' }))
    await waitFor(() => expect(m.publishRevision).toHaveBeenCalledWith('scout', 3))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Roll back to this' }))
    await waitFor(() => expect(m.rollbackTo).toHaveBeenCalledWith('scout', 1))
  })

  it('offers no Publish or Roll back to someone who may only draft', async () => {
    setup(false)
    await screen.findByTestId('definition-revisions')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Discard' })).toBeTruthy())
    expect(screen.queryByRole('button', { name: 'Publish' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Roll back to this' })).toBeNull()
  })
})
