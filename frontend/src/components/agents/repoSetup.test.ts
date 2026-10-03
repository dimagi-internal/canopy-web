import { describe, expect, it } from 'vitest'

import { cloneCommand, repoFolder } from './repoSetup'

describe('repoSetup', () => {
  it('clones into the folder named for the agent', () => {
    expect(repoFolder('jarvis')).toBe('~/emdash-projects/jarvis')
    expect(cloneCommand('jarvis', 'https://github.com/dimagi-internal/jarvis')).toBe(
      'git clone https://github.com/dimagi-internal/jarvis ~/emdash-projects/jarvis',
    )
  })
  it('has nothing to offer for an agent with no repo', () => {
    expect(cloneCommand('muse', '  ')).toBeNull()
  })
})
