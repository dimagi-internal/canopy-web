import { describe, it, expect } from 'vitest'
import { firstRunState, shouldOfferCreateForm } from './firstRun'

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
})

describe('shouldOfferCreateForm', () => {
  it('respects eligibility even when alwaysOfferForm is true (the F1 gate)', () => {
    // An invite-admitted user holding no membership may not create a workspace.
    // On /new-workspace, passing alwaysOfferForm=true should NOT bypass the
    // eligibility check — offering a form would 403.
    expect(shouldOfferCreateForm({ state: 'ready', canCreate: false, alwaysOfferForm: true })).toBe(
      false,
    )
  })

  it('offers the form on /new-workspace to an eligible user who already belongs somewhere', () => {
    expect(shouldOfferCreateForm({ state: 'ready', canCreate: true, alwaysOfferForm: true })).toBe(
      true,
    )
  })

  it('offers the form to an eligible user with no workspace', () => {
    expect(shouldOfferCreateForm({ state: 'can-create', canCreate: true, alwaysOfferForm: false }))
      .toBe(true)
  })

  it('does not offer the form to an ineligible user with no workspace', () => {
    expect(shouldOfferCreateForm({ state: 'needs-invite', canCreate: false, alwaysOfferForm: false }))
      .toBe(false)
  })
})
