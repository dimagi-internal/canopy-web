// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

vi.mock('@/api/agents', () => ({ deleteAgent: vi.fn(async () => undefined) }))

import { deleteAgent } from '@/api/agents'
import { DeleteAgentControl } from './DeleteAgentControl'

const del = deleteAgent as unknown as ReturnType<typeof vi.fn>

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('DeleteAgentControl', () => {
  it('stays disabled until the slug is typed, then deletes and returns to Agents', async () => {
    render(
      <MemoryRouter initialEntries={['/w/scouts/agents/scout/settings']}>
        <Routes>
          <Route path="/w/:workspace/agents/scout/settings" element={<DeleteAgentControl slug="scout" name="Scout" workspace="scouts" />} />
          <Route path="/w/:workspace/agents" element={<p>agents list</p>} />
        </Routes>
      </MemoryRouter>,
    )
    const button = screen.getByRole('button', { name: 'Delete Scout' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText("Type the agent's slug to confirm"), { target: { value: 'scou' } })
    expect(button.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText("Type the agent's slug to confirm"), { target: { value: 'scout' } })
    fireEvent.click(button)
    expect(await screen.findByText('agents list')).toBeTruthy()
    expect(del).toHaveBeenCalledWith('scout')
  })
})
