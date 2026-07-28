import { describe, expect, it } from 'vitest'
import { pageKeyFor } from 'canopy-ui/presence'
import { canopyPresenceRules } from './routes'

describe('canopyPresenceRules', () => {
  it('collapses an agent Workspace\'s nested sections onto one key, keeping the section as sub-location', () => {
    const inbox = pageKeyFor('canopy', '/w/dimagi/agents/echo/inbox', canopyPresenceRules)
    const tasks = pageKeyFor('canopy', '/w/dimagi/agents/echo/tasks', canopyPresenceRules)
    expect(inbox?.pageKey).toBe('canopy:dimagi:agent:echo')
    expect(tasks?.pageKey).toBe(inbox?.pageKey)
    expect(inbox?.subLocation).toBe('Inbox')
    expect(tasks?.subLocation).toBe('Tasks')
  })

  it('keeps different agents on different keys', () => {
    const echo = pageKeyFor('canopy', '/w/dimagi/agents/echo/inbox', canopyPresenceRules)
    const hal = pageKeyFor('canopy', '/w/dimagi/agents/hal/inbox', canopyPresenceRules)
    expect(echo?.pageKey).not.toBe(hal?.pageKey)
  })

  it('gives two different chat sessions different keys', () => {
    const a = pageKeyFor('canopy', '/w/dimagi/chat/abc-123', canopyPresenceRules)
    const b = pageKeyFor('canopy', '/w/dimagi/chat/def-456', canopyPresenceRules)
    expect(a?.pageKey).not.toBe(b?.pageKey)
    expect(a?.pageKey).toBe('canopy:dimagi:session:abc-123')
    expect(b?.pageKey).toBe('canopy:dimagi:session:def-456')
  })

  it('lands a global (non-tenant) page in the ~global sentinel namespace', () => {
    // The `~` prefix is a security boundary, not cosmetics: the server's
    // page-key parser only treats `~global` as "skip the membership gate",
    // and no client-assertable workspace slug can contain a `~`. A bare
    // `global` here would be a workspace name a user can create.
    const settings = pageKeyFor('canopy', '/settings', canopyPresenceRules)
    expect(settings?.pageKey).toBe('canopy:~global:settings')
  })

  it('never emits a bare "global" workspace segment from any global rule', () => {
    for (const path of [
      '/settings',
      '/system',
      '/insights',
      '/sessions',
      '/supervisor',
      '/schedules',
      '/activity',
      '/walkthrough/abc',
      '/review/abc',
    ]) {
      const loc = pageKeyFor('canopy', path, canopyPresenceRules)
      expect(loc?.pageKey.startsWith('canopy:~global:')).toBe(true)
    }
  })

  it('returns null for /invite/:token — a pending invitee has no roster to join', () => {
    expect(pageKeyFor('canopy', '/invite/abc123token', canopyPresenceRules)).toBeNull()
  })

  it('gives two different DDD runs of the same narrative different keys, distinct from the narrative editor', () => {
    const run1 = pageKeyFor('canopy', '/w/dimagi/ddd/bednet/run-001', canopyPresenceRules)
    const run2 = pageKeyFor('canopy', '/w/dimagi/ddd/bednet/run-002', canopyPresenceRules)
    const narrative = pageKeyFor('canopy', '/w/dimagi/ddd/bednet', canopyPresenceRules)
    expect(run1?.pageKey).not.toBe(run2?.pageKey)
    expect(run1?.pageKey).not.toBe(narrative?.pageKey)
  })

  it('gives two different shareouts periods different keys', () => {
    const a = pageKeyFor('canopy', '/w/dimagi/shareouts/2026-07', canopyPresenceRules)
    const b = pageKeyFor('canopy', '/w/dimagi/shareouts/2026-06', canopyPresenceRules)
    expect(a?.pageKey).not.toBe(b?.pageKey)
  })

  it('routes the bare workspace index to a "projects" resource', () => {
    const bare = pageKeyFor('canopy', '/w/dimagi', canopyPresenceRules)
    const trailing = pageKeyFor('canopy', '/w/dimagi/', canopyPresenceRules)
    expect(bare?.pageKey).toBe('canopy:dimagi:projects')
    expect(trailing?.pageKey).toBe('canopy:dimagi:projects')
  })

  it('namespaces by app so two apps never collide on the same tenant route', () => {
    const canopy = pageKeyFor('canopy', '/w/dimagi/timeline', canopyPresenceRules)
    const other = pageKeyFor('ace', '/w/dimagi/timeline', canopyPresenceRules)
    expect(canopy?.pageKey).toBe('canopy:dimagi:timeline')
    expect(other?.pageKey).toBe('ace:dimagi:timeline')
  })
})
