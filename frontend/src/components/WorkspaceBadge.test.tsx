// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { WorkspaceBadge } from './WorkspaceBadge'

afterEach(cleanup)

function onPage(path: string, row: string | null) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/w/:workspace/agents" element={<WorkspaceBadge workspace={row} />} />
        <Route path="/supervisor" element={<WorkspaceBadge workspace={row} />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('WorkspaceBadge', () => {
  it('marks a row from a workspace below the page workspace', () => {
    onPage('/w/dimagi/agents', 'connect')
    expect(screen.getByTestId('workspace-badge').textContent).toBe('connect')
  })

  it("stays out of the way for the page's own rows", () => {
    onPage('/w/dimagi/agents', 'dimagi')
    expect(screen.queryByTestId('workspace-badge')).toBeNull()
  })

  it('shows nothing when the page or the row has no workspace', () => {
    onPage('/supervisor', 'connect')
    expect(screen.queryByTestId('workspace-badge')).toBeNull()
    cleanup()
    onPage('/w/dimagi/agents', null)
    expect(screen.queryByTestId('workspace-badge')).toBeNull()
  })
})
