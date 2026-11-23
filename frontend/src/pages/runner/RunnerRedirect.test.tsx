// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getRunner = vi.fn()
vi.mock('@/api/harness', () => ({ getRunner: (id: string) => getRunner(id) }))

import { RunnerRedirect } from './RunnerRedirect'

function at(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/runners/:runnerId" element={<RunnerRedirect />} />
        <Route path="/runners/:runnerId/:tab" element={<RunnerRedirect />} />
        <Route path="/w/:workspace/runners/:runnerId/:tab" element={<p>runner page</p>} />
        <Route path="/supervisor" element={<p>supervisor</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('RunnerRedirect', () => {
  beforeEach(() => {
    getRunner.mockReset()
  })
  afterEach(cleanup)

  it("forwards the Runner app's Settings link to the tab under the runner's workspace", async () => {
    getRunner.mockResolvedValue({ id: 'r1', workspace: 'dimagi' })
    at('/runners/r1/settings')
    expect(await screen.findByText('runner page')).toBeTruthy()
    expect(getRunner).toHaveBeenCalledWith('r1')
  })

  it('lands on the supervisor when the runner cannot be read', async () => {
    getRunner.mockRejectedValue(new Error('404'))
    at('/runners/gone')
    expect(await screen.findByText('supervisor')).toBeTruthy()
  })
})
