// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { AuthContext } from './AuthProvider'
import { useMemoryVisible } from './memoryRollout'

afterEach(cleanup)

function Probe() {
  return <p>{useMemoryVisible() ? 'shown' : 'hidden'}</p>
}

function as(flags: { memory_enabled?: boolean; memory_paused?: boolean }) {
  const user = { email: 'x@dimagi.com', name: 'X', avatar_url: '', can_create_workspace: true, ...flags }
  render(
    <AuthContext.Provider value={{ status: 'authenticated', user: user as never }}>
      <Probe />
    </AuthContext.Provider>,
  )
  return screen.getByRole('paragraph').textContent
}

describe('useMemoryVisible — memory is on only for the people the rollout names', () => {
  it('hides every memory surface for someone outside it', () => {
    expect(as({ memory_enabled: false, memory_paused: false })).toBe('hidden')
  })

  it('shows them to someone in it', () => {
    expect(as({ memory_enabled: true })).toBe('shown')
  })

  it('keeps "Your memory" reachable when canopy already holds some of theirs', () => {
    expect(as({ memory_enabled: false, memory_paused: true })).toBe('shown')
  })

  it('an older server that says nothing hides them (fail closed)', () => {
    expect(as({})).toBe('hidden')
  })
})
