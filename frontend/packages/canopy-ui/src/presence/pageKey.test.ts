import { describe, expect, it } from 'vitest'
import { pageKeyFor, type RouteRule } from './pageKey'

const ACE_RULES: RouteRule[] = [
  {
    pattern: /^\/w\/([^/]+)\/opps\/([^/]+)\/runs\/([^/]+)\/steps\/([^/]+)/,
    build: (m) => ({ workspace: m[1], resource: `opp:${m[2]}/${m[3]}`, subLocation: m[4] }),
  },
  {
    pattern: /^\/w\/([^/]+)\/opps\/([^/]+)\/runs\/([^/]+)/,
    build: (m) => ({ workspace: m[1], resource: `opp:${m[2]}/${m[3]}`, subLocation: 'run overview' }),
  },
  {
    pattern: /^\/w\/([^/]+)\/activity/,
    build: (m) => ({ workspace: m[1], resource: 'activity', subLocation: 'Activity' }),
  },
]

describe('pageKeyFor', () => {
  it('collapses every step of a run onto one key, keeping the step as sub-location', () => {
    const a = pageKeyFor('ace', '/w/dimagi-team/opps/bednet/runs/run-001/steps/idea-to-pdd', ACE_RULES)
    const b = pageKeyFor('ace', '/w/dimagi-team/opps/bednet/runs/run-001', ACE_RULES)
    expect(a?.pageKey).toBe('ace:dimagi-team:opp:bednet/run-001')
    expect(b?.pageKey).toBe(a?.pageKey)
    expect(a?.subLocation).toBe('idea-to-pdd')
    expect(b?.subLocation).toBe('run overview')
  })

  it('keeps different runs of the same opp on different keys', () => {
    const a = pageKeyFor('ace', '/w/dimagi-team/opps/bednet/runs/run-001', ACE_RULES)
    const b = pageKeyFor('ace', '/w/dimagi-team/opps/bednet/runs/run-002', ACE_RULES)
    expect(a?.pageKey).not.toBe(b?.pageKey)
  })

  it('namespaces by app so two apps never collide', () => {
    const ace = pageKeyFor('ace', '/w/dimagi-team/activity', ACE_RULES)
    const canopy = pageKeyFor('canopy', '/w/dimagi-team/activity', ACE_RULES)
    expect(ace?.pageKey).toBe('ace:dimagi-team:activity')
    expect(canopy?.pageKey).toBe('canopy:dimagi-team:activity')
  })

  it('returns null for unmatched routes rather than a catch-all key', () => {
    expect(pageKeyFor('ace', '/totally/unknown', ACE_RULES)).toBeNull()
  })

  it('is order-sensitive: the first matching rule wins', () => {
    const loose: RouteRule[] = [
      { pattern: /^\/w\/([^/]+)\/opps/, build: (m) => ({ workspace: m[1], resource: 'opps', subLocation: 'Opps' }) },
      ...ACE_RULES,
    ]
    expect(pageKeyFor('ace', '/w/x/opps/bednet/runs/run-001', loose)?.pageKey).toBe('ace:x:opps')
  })
})
