import { describe, expect, it } from 'vitest'
import { agentDisplayName } from './TasksBoard'

describe('agentDisplayName', () => {
  it('names the board’s own agent, not a hardcoded one', () => {
    expect(agentDisplayName('jarvis')).toBe('Jarvis')
    expect(agentDisplayName('hal')).toBe('Hal')
  })

  it('falls back when the slug is missing', () => {
    expect(agentDisplayName('')).toBe('Agent')
  })
})
