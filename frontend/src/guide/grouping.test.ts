import { describe, it, expect } from 'vitest'
import { guideGroups } from './grouping'
import { SURFACES } from './surfaces'

describe('guideGroups', () => {
  it('places every surface in exactly one group', () => {
    const grouped = guideGroups().flatMap((g) => g.surfaces.map((s) => s.path))
    expect(grouped.length).toBe(SURFACES.length)
    expect(new Set(grouped).size).toBe(SURFACES.length)
  })

  it('uses the nav group labels', () => {
    const labels = guideGroups().map((g) => g.label)
    expect(labels).toContain('Agents')
    expect(labels).toContain('Work')
  })

  it('never drops a surface, even one nothing claims', () => {
    const groups = guideGroups([
      { path: '/nowhere-at-all', title: 'Orphan', audience: 'Anyone', what: 'x' },
    ])
    expect(groups).toEqual([
      { label: 'Elsewhere', surfaces: [expect.objectContaining({ path: '/nowhere-at-all' })] },
    ])
  })

  it('groups the agent rail rather than letting Agents absorb it', () => {
    // The rail sits under /w/:workspace/agents/:slug/, and nav has
    // /w/:workspace/agents — so without group-first assignment every rail
    // section would land in Agents and this group would be empty.
    const rail = guideGroups().find((g) => g.label === 'Agent workspace')
    // Projects, one project, a contributor page, Tasks, Turns, Huddles, Schedules,
    // Mail, Skills, skill history, Capabilities, Settings, Connect your Google account.
    expect(rail?.surfaces.length).toBe(13)
    const fleet = guideGroups().find((g) => g.label === 'Agents')
    expect(fleet?.surfaces.map((s) => s.path)).toContain('/w/:workspace/agents')
    expect(fleet?.surfaces.map((s) => s.path)).not.toContain('/w/:workspace/agents/:slug/tasks')
  })

  it('groups the shared-by-link surfaces together', () => {
    const shared = guideGroups().find((g) => g.label === 'Shared by link')
    expect(shared?.surfaces.map((s) => s.path)).toEqual(
      expect.arrayContaining(['/w/:workspace/share/:token', '/w/:workspace/storyboard/:slug', '/w/:workspace/walkthrough/:id']),
    )
  })

  it('puts a detail route with its list route', () => {
    const chats = guideGroups().find((g) => g.surfaces.some((s) => s.path === '/w/:workspace/sessions'))
    expect(chats?.surfaces.map((s) => s.path)).toContain('/w/:workspace/sessions/:id')
  })

  it('files every page under a menu, its home, or its own group', () => {
    // Elsewhere is the safety net, not a place: a page lands there only when the
    // registry forgot to say where it lives.
    const elsewhere = guideGroups().find((g) => g.label === 'Elsewhere')
    const lost = elsewhere?.surfaces.map((s) => s.path) ?? []
    expect(lost, `give these a home or group in guide/surfaces.ts: ${lost.join(', ')}`).toEqual([])
  })

  it('files every agent-console section in the agent workspace', () => {
    const rail = SURFACES.filter((s) => s.path.startsWith('/w/:workspace/agents/:slug/'))
    expect(rail.filter((s) => s.group !== 'Agent workspace').map((s) => s.path)).toEqual([])
  })

  it('files a page no menu links under the page it is reached from', () => {
    const health = guideGroups().find((g) => g.label === 'Workspace')
    expect(health?.surfaces.map((s) => s.path)).toEqual(
      expect.arrayContaining(['/w/:workspace/analytics', '/w/:workspace/network']),
    )
    const home = guideGroups().find((g) => g.label === 'Home')
    expect(home?.surfaces.map((s) => s.path)).toEqual(['/home', '/schedules', '/activity'])
  })
})
