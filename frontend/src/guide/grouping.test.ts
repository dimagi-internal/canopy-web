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
    expect(labels).toContain('Work')
    expect(labels).toContain('Fleet')
  })

  it('never drops a surface, even one nothing claims', () => {
    const groups = guideGroups([
      { path: '/nowhere-at-all', title: 'Orphan', audience: 'Anyone', what: 'x' },
    ])
    expect(groups).toEqual([
      { label: 'Elsewhere', surfaces: [expect.objectContaining({ path: '/nowhere-at-all' })] },
    ])
  })

  it('clusters the agent rail rather than letting Fleet absorb it', () => {
    // The rail sits under /w/:workspace/agents/:slug/, and nav has
    // /w/:workspace/agents — so without cluster-first assignment all ten
    // sections would land in Fleet and this group would be empty.
    const rail = guideGroups().find((g) => g.label === 'Agent workspace')
    expect(rail?.surfaces.length).toBe(10)
    const fleet = guideGroups().find((g) => g.label === 'Fleet')
    expect(fleet?.surfaces.map((s) => s.path)).toContain('/w/:workspace/agents')
    expect(fleet?.surfaces.map((s) => s.path)).not.toContain('/w/:workspace/agents/:slug/inbox')
  })

  it('groups the no-login shared links together', () => {
    const shared = guideGroups().find((g) => g.label === 'Shared links (no login)')
    expect(shared?.surfaces.map((s) => s.path)).toEqual(
      expect.arrayContaining(['/share/:token', '/storyboard/:slug', '/walkthrough/:id']),
    )
  })

  it('puts a detail route with its list route', () => {
    const chats = guideGroups().find((g) => g.surfaces.some((s) => s.path === '/w/:workspace/chat'))
    expect(chats?.surfaces.map((s) => s.path)).toContain('/w/:workspace/chat/:id')
  })

  it('does not let the tenant index swallow every tenant path', () => {
    // /w/:workspace prefixes every tenant route; if it prefix-matched, the group
    // holding Projects would absorb the entire app.
    const work = guideGroups().find((g) => g.surfaces.some((s) => s.path === '/w/:workspace'))
    expect(work?.surfaces.map((s) => s.path)).not.toContain('/w/:workspace/members')
  })

  it('keeps Elsewhere a small remainder, not a dumping ground', () => {
    const elsewhere = guideGroups().find((g) => g.label === 'Elsewhere')
    // Measured at ~4 genuinely uncategorised surfaces. A jump here means a new
    // cluster needs a rule, not that the bucket should grow.
    expect(elsewhere?.surfaces.length ?? 0).toBeLessThanOrEqual(8)
  })
})
