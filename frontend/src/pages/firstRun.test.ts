import { describe, it, expect } from 'vitest'
import { firstRunState } from './firstRun'

describe('firstRunState', () => {
  it('is loading while the workspace list is unresolved, regardless of eligibility', () => {
    // Two cases with opposite `canCreate` values, both loading — this is the
    // mutation guard: deleting the `loading` check entirely would make the
    // first of these return 'can-create' instead.
    expect(firstRunState({ loading: true, workspaceCount: 0, canCreate: true })).toBe('loading')
    expect(firstRunState({ loading: true, workspaceCount: 0, canCreate: false })).toBe('loading')
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

  it('prefers loading over ready when the workspace list has not resolved yet', () => {
    // Even though workspaceCount > 0 would normally mean 'ready', a still-loading
    // list means that count isn't trustworthy yet.
    expect(firstRunState({ loading: true, workspaceCount: 1, canCreate: true })).toBe('loading')
  })

  it('still reports ready for a member — /new-workspace opts out via a prop, not this fn', () => {
    // Documents the seam: firstRunState stays a pure description of the user's
    // standing. The route decides whether to show a form anyway.
    expect(firstRunState({ loading: false, workspaceCount: 3, canCreate: true })).toBe('ready')
  })
})
