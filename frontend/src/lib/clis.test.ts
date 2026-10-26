import { describe, expect, it } from 'vitest'
import { CLIS, cliLabel } from './clis'

describe('cliLabel', () => {
  it('names each CLI the way a person reads it, and an unknown one as it is', () => {
    expect(cliLabel('claude-code')).toBe('Claude Code')
    expect(cliLabel('codex')).toBe('Codex')
    expect(cliLabel('gpt')).toBe('gpt')
  })

  it('lists every CLI the server knows, Claude Code first', () => {
    expect(CLIS).toEqual(['claude-code', 'codex'])
  })
})
