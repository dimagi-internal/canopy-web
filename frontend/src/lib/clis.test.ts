import { describe, expect, it } from 'vitest'
import { cliLabel, runnerClis } from './clis'

describe('cliLabel', () => {
  it('names the known CLIs and shows anything else as it is', () => {
    expect(cliLabel('claude-code')).toBe('Claude Code')
    expect(cliLabel('codex')).toBe('Codex')
    expect(cliLabel('gpt')).toBe('gpt')
  })
})

describe('runnerClis', () => {
  it('reads a box that reports none as Claude Code alone', () => {
    expect(runnerClis({ profiles: 3, envelope: 2 }, true)).toEqual([
      { cli: 'claude-code', signedIn: true, confines: true, version: '' },
    ])
  })
  it('reads what a box reports', () => {
    const caps = { clis: { codex: { signed_in: true, profiles: 0, envelope: 0, version: '0.164.0' } } }
    expect(runnerClis(caps, true)).toEqual([
      { cli: 'codex', signedIn: true, confines: false, version: '0.164.0' },
    ])
  })
})
