import { describe, it, expect } from 'vitest'
import { firstRunState } from './firstRun'

describe('firstRunState', () => {
  it('is loading while either the workspace list or me is unresolved', () => {
    expect(firstRunState({ loading: true, workspaceCount: 0, canCreate: null })).toBe('loading')
    expect(firstRunState({ loading: false, workspaceCount: 0, canCreate: null })).toBe('loading')
  })

  it('is ready once the user has at least one workspace', () => {
    expect(firstRunState({ loading: false, workspaceCount: 1, canCreate: false })).toBe('ready')
  })

  it('offers creation to an eligible user with no workspace', () => {
    expect(firstRunState({ loading: false, workspaceCount: 0, canCreate: true })).toBe('can-create')
  })

  it('asks an ineligible user for an invite instead of offering a button that 403s', () => {
    expect(firstRunState({ loading: false, workspaceCount: 0, canCreate: false })).toBe('needs-invite')
  })

  it('prefers ready over creation when the user already belongs somewhere', () => {
    // Guards the ordering: an eligible user WITH a workspace must be routed on,
    // not parked on the first-run screen.
    expect(firstRunState({ loading: false, workspaceCount: 2, canCreate: true })).toBe('ready')
  })
})
