// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import {
  HELP_ITEMS,
  NAV,
  activeHref,
  entryItems,
  isNavEntryActive,
  isNavItemActive,
  resolveNav,
  switchWorkspacePath,
  type ResolvedNavEntry,
} from './nav'

const authed = { isAuthed: true, active: 'connect', role: 'owner' }
const label = (e: ResolvedNavEntry) => (e.kind === 'link' ? e.item.label : e.label)

describe('NAV', () => {
  it('answers the five questions a person arrives with, plus Sessions', () => {
    // Home: what needs me. Agents: what they are doing. Work: the tasks and
    // projects. Demos: what the work produces. Workspace: who is in it and how
    // it is set up.
    expect(NAV.map((e) => (e.kind === 'link' ? e.item.label : e.label))).toEqual([
      'Home', 'Sessions', 'Agents', 'Work', 'Demos', 'Workspace',
    ])
  })

  it('files each destination once across the header and the help menu', () => {
    const items = [...NAV.flatMap((e) => (e.kind === 'link' ? [e.item] : e.items)), ...HELP_ITEMS]
    const keys = items.map((i) => `${i.tenant}:${i.path}`)
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('keeps help out of the workspace menu — it is the same in every workspace', () => {
    expect(HELP_ITEMS.map((i) => i.label)).toEqual(['Guide', 'Get started', 'System'])
    const workspace = NAV.find((e) => e.kind === 'menu' && e.label === 'Workspace')!
    expect(entryItems(resolveNav(authed).find((e) => label(e) === 'Workspace')!).map((i) => i.label))
      .not.toContain('Guide')
    expect(workspace.kind).toBe('menu')
  })

  it('scopes every Agents entry to the workspace — no silent cross-workspace twin', () => {
    const agents = NAV.find((e) => e.kind === 'menu' && e.label === 'Agents')
    expect(agents?.kind === 'menu' && agents.items.every((i) => i.tenant)).toBe(true)
  })
})

describe('resolveNav', () => {
  it('offers admin pages only to a role that can read them, so an editor never lands on a refusal', () => {
    const labels = (role: string | null) =>
      resolveNav({ ...authed, role }).flatMap((e) => entryItems(e).map((i) => i.label))
    expect(labels('admin')).toEqual(expect.arrayContaining(['Health', 'Runners']))
    expect(labels('editor')).not.toContain('Health')
    expect(labels('editor')).not.toContain('Runners')
    expect(labels('editor')).toContain('New agent')
    expect(labels('viewer')).not.toContain('New agent')
  })

  it('resolves tenant items under the active workspace and leaves Home at root', () => {
    const nav = resolveNav(authed)
    expect(nav[0]).toEqual({ kind: 'link', item: { href: '/home', label: 'Home' } })
    expect(nav[1]).toEqual({ kind: 'link', item: { href: '/w/connect/chat', label: 'Sessions' } })
    const work = nav.find((e) => label(e) === 'Work')!
    expect(entryItems(work)[0]).toEqual({ href: '/w/connect/tasks', label: 'Tasks' })
  })

  it('omits tenant items before the workspace resolves, rather than linking to /w//…', () => {
    const nav = resolveNav({ isAuthed: true, active: null })
    const hrefs = nav.flatMap((e) => entryItems(e).map((i) => i.href))
    expect(hrefs).toEqual(['/home'])
  })

  it('shows an anonymous visitor no nav at all', () => {
    expect(resolveNav({ isAuthed: false, active: 'connect' })).toEqual([])
  })
})

describe('isNavItemActive', () => {
  it('matches the exact path', () => {
    expect(isNavItemActive('/home', '/home')).toBe(true)
  })

  it('matches a nested route below the item', () => {
    expect(isNavItemActive('/w/connect/agents', '/w/connect/agents/hal/inbox')).toBe(true)
  })

  it('does not match a sibling that merely shares a prefix', () => {
    expect(isNavItemActive('/w/connect/agents', '/w/connect/agents-archive')).toBe(false)
  })

  it('matches the workspace index only exactly', () => {
    expect(isNavItemActive('/w/connect', '/w/connect')).toBe(true)
    expect(isNavItemActive('/w/connect', '/w/connect/timeline')).toBe(false)
  })
})

describe('active entry', () => {
  const nav = resolveNav(authed)
  const hits = (pathname: string) => nav.filter((e) => isNavEntryActive(e, nav, pathname)).map(label)

  it('marks exactly one entry for any route the header owns', () => {
    const expected: Record<string, string> = {
      '/home': 'Home',
      '/w/connect/chat/abc': 'Sessions',
      '/w/connect/agents/hal/turns': 'Agents',
      '/w/connect/agents/new': 'Agents',
      '/w/connect/activity': 'Agents',
      '/w/connect/schedules': 'Agents',
      '/w/connect/huddles/4': 'Agents',
      '/w/connect/settings/topology/runners': 'Agents',
      '/w/connect/projects': 'Work',
      '/w/connect/tasks/CON-4': 'Work',
      '/w/connect/triage': 'Work',
      '/w/connect/ddd/onboarding': 'Demos',
      '/w/connect/storyboards': 'Demos',
      '/w/connect/shareouts/2026-09-01': 'Demos',
      '/w/connect/settings': 'Workspace',
      '/w/connect/settings/slack': 'Workspace',
      '/w/connect/settings/members': 'Workspace',
      '/w/connect/timeline': 'Workspace',
    }
    for (const [pathname, entry] of Object.entries(expected)) {
      expect(hits(pathname), pathname).toEqual([entry])
    }
  })

  it('picks the most specific item: Runners, not Settings, on the topology pages', () => {
    expect(activeHref(nav, '/w/connect/settings/topology/runners')).toBe('/w/connect/settings/topology')
    expect(activeHref(nav, '/w/connect/settings/members')).toBe('/w/connect/settings/members')
  })

  it('marks nothing on a personal page or a public share page', () => {
    for (const pathname of ['/settings', '/supervisor', '/w/connect/storyboard/oes-supply']) {
      expect(hits(pathname), pathname).toEqual([])
    }
  })
})

describe('switchWorkspacePath', () => {
  it('keeps the page you are on, in the other workspace', () => {
    expect(switchWorkspacePath('/w/dimagi/health', 'marshellis')).toBe('/w/marshellis/health')
    expect(switchWorkspacePath('/w/dimagi/analytics', 'marshellis')).toBe('/w/marshellis/analytics')
    expect(switchWorkspacePath('/w/dimagi/settings/topology/runners', 'm')).toBe('/w/m/settings/topology')
    // A page about one thing names something of the old workspace: its list instead.
    expect(switchWorkspacePath('/w/dimagi/agents/hal/settings', 'm')).toBe('/w/m/agents')
    expect(switchWorkspacePath('/w/dimagi/chat/abc', 'm')).toBe('/w/m/chat')
    expect(switchWorkspacePath('/w/dimagi/threads/7', 'm')).toBe('/w/m/huddles')
    expect(switchWorkspacePath('/w/dimagi', 'm')).toBe('/w/m/agents')
    expect(switchWorkspacePath('/home', 'm')).toBe('/w/m/agents')
  })

  it('never switches to a section that has no page of its own', async () => {
    const { routeTable } = await import('../../router')
    const { flattenRoutePaths } = await import('../../guide/coverage')
    const all = flattenRoutePaths(routeTable).filter((p) => p.startsWith('/w/:workspace/'))
    const routed = new Set(all.map((p) => p.replace('/w/:workspace/', '')))
    for (const p of all) {
      const target = switchWorkspacePath(p.replace(/:workspace/, 'x').replace(/:[a-zA-Z]+/g, 'v'), 'y')
      expect(routed.has(target.replace('/w/y/', '')), `${p} -> ${target}`).toBe(true)
    }
  })
})
