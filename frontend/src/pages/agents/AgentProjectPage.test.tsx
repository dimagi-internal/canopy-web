// @vitest-environment jsdom
//
// A project has one page, at its address. The agent's alias for it (P3) and the
// older /w/:ws/projects/:agent/:ref address both look it up and go there.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const getProject = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getProject: (...a: unknown[]) => getProject(...a),
}))

const { AgentProjectPage } = await import('./AgentProjectPage')
const { ProjectPageRedirect } = await import('@/router')
const { AgentApiError } = await import('@/api/agents')

afterEach(() => {
  cleanup()
  getProject.mockReset()
})

function at(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/w/:workspace/agents/:slug/projects/:ref" element={<AgentProjectPage />} />
        <Route path="/w/:workspace/agents/:slug/projects/:ref/people/:person" element={<p>contributor page</p>} />
        <Route path="/w/:workspace/projects/:agent/:ref" element={<ProjectPageRedirect />} />
        <Route path="/w/:workspace/projects/:agent/:ref/people/:person" element={<ProjectPageRedirect />} />
        <Route path="/w/:workspace/projects/:handle" element={<p>project page</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('the agent link to a project', () => {
  it("goes to the project's one page", async () => {
    getProject.mockResolvedValue({ url: '/w/dimagi/projects/idm-talk' })
    at('/w/dimagi/agents/eva/projects/P3')
    expect(await screen.findByText('project page')).toBeTruthy()
    expect(getProject).toHaveBeenCalledWith('eva', 'P3')
  })

  it('says so when the agent has no such project', async () => {
    getProject.mockRejectedValue(new AgentApiError(404, 'not found', { detail: 'not found' }))
    at('/w/dimagi/agents/eva/projects/P99')
    expect(await screen.findByText('No project P99 on eva.')).toBeTruthy()
  })

  it('takes the older workspace address there too, and a contributor to their page', async () => {
    getProject.mockResolvedValue({ url: '/w/dimagi/projects/idm-talk' })
    at('/w/dimagi/projects/eva/P3')
    expect(await screen.findByText('project page')).toBeTruthy()
    cleanup()
    at('/w/dimagi/projects/eva/P3/people/4')
    expect(await screen.findByText('contributor page')).toBeTruthy()
  })
})
