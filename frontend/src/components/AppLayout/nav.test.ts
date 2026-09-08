import { describe, expect, it } from 'vitest'
import {
  NAV_GROUPS,
  isNavGroupActive,
  isNavItemActive,
  resolveNavGroups,
} from './nav'

const authed = { isAuthed: true, active: 'connect' }

describe('NAV_GROUPS', () => {
  it('keeps every destination the flat nav carried', () => {
    // The row this replaced held 15 links. Grouping is meant to reorganize the
    // header, never to quietly drop a surface out of it.
    const labels = NAV_GROUPS.flatMap((g) => g.items.map((i) => i.label))
    expect(labels.sort()).toEqual(
      [
        'Activity', 'Agents', 'Chats', 'DDD', 'Inbound', 'Insights', 'Members',
        'Projects', 'Schedule', 'Sessions', 'Shareouts', 'Supervisor', 'System',
        'Timeline', 'Walkthroughs',
      ].sort(),
    )
  })

  it('files each destination in exactly one group', () => {
    const hrefs = NAV_GROUPS.flatMap((g) => g.items.map((i) => `${i.tenant}:${i.path}`))
    expect(new Set(hrefs).size).toBe(hrefs.length)
  })

  it('fits the header — four triggers, which is what buys back the room', () => {
    expect(NAV_GROUPS.map((g) => g.label)).toEqual(['Work', 'Fleet', 'Demos', 'Workspace'])
  })
})

describe('resolveNavGroups', () => {
  it('resolves tenant items under the active workspace', () => {
    const work = resolveNavGroups(authed).find((g) => g.label === 'Work')!
    expect(work.items).toEqual([
      { href: '/w/connect', label: 'Projects' },
      { href: '/w/connect/chat', label: 'Chats' },
      { href: '/insights', label: 'Insights' },
    ])
  })

  it('leaves global items on their absolute path', () => {
    const fleet = resolveNavGroups(authed).find((g) => g.label === 'Fleet')!
    expect(fleet.items.map((i) => i.href)).toEqual([
      '/supervisor', '/w/connect/agents', '/activity', '/schedules',
    ])
  })

  it('omits tenant items before the workspace resolves, rather than linking to /w//…', () => {
    const groups = resolveNavGroups({ isAuthed: true, active: null })
    const hrefs = groups.flatMap((g) => g.items.map((i) => i.href))
    expect(hrefs.some((h) => h.includes('/w//'))).toBe(false)
    expect(hrefs).toEqual(['/insights', '/supervisor', '/activity', '/schedules', '/sessions', '/system'])
  })

  it('drops a group whose items are all tenant-scoped while the workspace is unknown', () => {
    // Demos is DDD + Walkthroughs, both tenant items — an empty menu would be
    // a trigger that opens onto nothing.
    const labels = resolveNavGroups({ isAuthed: true, active: null }).map((g) => g.label)
    expect(labels).not.toContain('Demos')
    expect(labels).toEqual(['Work', 'Fleet', 'Workspace'])
  })

  it('shows an anonymous visitor no nav at all', () => {
    // Every destination is behind the login gate; the links would bounce to
    // sign-in. Public link routes render the shell but must not offer them.
    expect(resolveNavGroups({ isAuthed: false, active: 'connect' })).toEqual([])
  })
})

describe('isNavItemActive', () => {
  it('matches the exact path', () => {
    expect(isNavItemActive('/activity', '/activity')).toBe(true)
  })

  it('matches a nested route below the item', () => {
    expect(isNavItemActive('/w/connect/agents', '/w/connect/agents/hal/inbox')).toBe(true)
  })

  it('does not match a sibling that merely shares a prefix', () => {
    expect(isNavItemActive('/w/connect/agents', '/w/connect/agents-archive')).toBe(false)
  })

  it('matches the workspace index only exactly', () => {
    // /w/<slug> prefixes every tenant route, so a prefix match would light
    // Projects up on every page in the workspace.
    expect(isNavItemActive('/w/connect', '/w/connect')).toBe(true)
    expect(isNavItemActive('/w/connect', '/w/connect/timeline')).toBe(false)
  })
})

describe('isNavGroupActive', () => {
  const groups = resolveNavGroups(authed)
  const group = (label: string) => groups.find((g) => g.label === label)!

  it('marks the group holding the current route', () => {
    expect(isNavGroupActive(group('Fleet'), '/w/connect/agents/hal/turns')).toBe(true)
    expect(isNavGroupActive(group('Workspace'), '/w/connect/shareouts/2026-09-01')).toBe(true)
  })

  it('marks Work on the workspace index', () => {
    expect(isNavGroupActive(group('Work'), '/w/connect')).toBe(true)
  })

  it('does not mark Work on some other tenant page', () => {
    // The regression the exact-match carve-out exists to prevent: Projects is
    // /w/<slug>, a prefix of every tenant route.
    expect(isNavGroupActive(group('Work'), '/w/connect/members')).toBe(false)
    expect(isNavGroupActive(group('Work'), '/w/connect/ddd/onboarding')).toBe(false)
  })

  it('marks exactly one group for any given route', () => {
    for (const pathname of [
      '/w/connect', '/w/connect/chat', '/insights', '/supervisor',
      '/w/connect/agents', '/activity', '/schedules', '/w/connect/ddd',
      '/w/connect/walkthroughs', '/w/connect/shareouts', '/w/connect/timeline',
      '/sessions', '/w/connect/members', '/w/connect/inbound', '/system',
    ]) {
      const hits = groups.filter((g) => isNavGroupActive(g, pathname)).map((g) => g.label)
      expect(hits, pathname).toHaveLength(1)
    }
  })

  it('marks no group on a route the nav does not own', () => {
    expect(groups.some((g) => isNavGroupActive(g, '/settings'))).toBe(false)
  })
})
