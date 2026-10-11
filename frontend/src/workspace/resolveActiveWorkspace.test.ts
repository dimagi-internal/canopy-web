import { describe, it, expect } from 'vitest'
import { resolveActiveWorkspace } from './resolveActiveWorkspace'

const wss = [{ slug: 'dimagi' }, { slug: 'acme' }]

describe('resolveActiveWorkspace', () => {
  it('uses the URL slug when it is a membership', () => {
    expect(resolveActiveWorkspace(wss, 'acme')).toBe('acme')
  })

  it('falls back to the first membership when URL slug is absent', () => {
    expect(resolveActiveWorkspace(wss, null)).toBe('dimagi')
  })

  it('ignores a URL slug that is not a membership', () => {
    expect(resolveActiveWorkspace(wss, 'ghost')).toBe('dimagi')
  })

  it('off a tenant route, keeps the workspace last chosen', () => {
    expect(resolveActiveWorkspace(wss, null, 'acme')).toBe('acme')
  })

  it('the URL beats the remembered workspace, and a stale memory is ignored', () => {
    expect(resolveActiveWorkspace(wss, 'dimagi', 'acme')).toBe('dimagi')
    expect(resolveActiveWorkspace(wss, null, 'left-it')).toBe('dimagi')
  })

  it('returns null with no memberships', () => {
    expect(resolveActiveWorkspace([], null)).toBeNull()
    expect(resolveActiveWorkspace([], 'anything')).toBeNull()
  })
})
