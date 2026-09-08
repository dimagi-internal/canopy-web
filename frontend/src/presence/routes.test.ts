import { describe, expect, it } from 'vitest'
import { pageKeyFor } from 'canopy-ui/presence'
import { canopyPresenceRules } from './routes'

const key = (path: string) => pageKeyFor('canopy', path, canopyPresenceRules)

describe('canopyPresenceRules — which pages get presence at all', () => {
  it('covers the surfaces where two people can silently clobber each other', () => {
    expect(key('/w/dimagi/chat/abc-123')).not.toBeNull()
    expect(key('/w/dimagi/ddd/bednet')).not.toBeNull()
    expect(key('/w/dimagi/ddd/bednet/run-001')).not.toBeNull()
    expect(key('/review/abc')).not.toBeNull()
  })

  it('leaves read surfaces with no roster, so the badge never reaches their header', () => {
    // A route with no rule yields null, which opens no socket and renders no
    // badge. These all used to have rules; presence on them was noise, since
    // nobody can collide with anyone by reading.
    for (const path of [
      '/w/dimagi',                       // workspace index (Projects)
      '/w/dimagi/',
      '/w/dimagi/members',
      '/w/dimagi/timeline',
      '/w/dimagi/walkthroughs',
      '/w/dimagi/inbound',
      '/w/dimagi/chat',                  // the chat LIST, not a session
      '/w/dimagi/ddd',                   // the DDD LIST, not a narrative
      '/w/dimagi/agents',
      '/w/dimagi/agents/echo/inbox',     // an Item decision is atomic + logged
      '/w/dimagi/shareouts',
      '/w/dimagi/shareouts/2026-07',
      '/w/dimagi/activity',
      '/walkthrough/abc',
      '/supervisor',
      '/insights',
      '/sessions',
      '/activity',
      '/schedules',
      '/system',
      '/settings',
    ]) {
      expect(key(path), path).toBeNull()
    }
  })

  it('returns null for /invite/:token — a pending invitee has no roster to join', () => {
    expect(key('/invite/abc123token')).toBeNull()
  })
})

describe('canopyPresenceRules — keying', () => {
  it('gives two different chat sessions different keys', () => {
    expect(key('/w/dimagi/chat/abc-123')?.pageKey).toBe('canopy:dimagi:session:abc-123')
    expect(key('/w/dimagi/chat/def-456')?.pageKey).toBe('canopy:dimagi:session:def-456')
  })

  it('gives two different DDD runs of the same narrative different keys, distinct from the narrative editor', () => {
    const run1 = key('/w/dimagi/ddd/bednet/run-001')
    const run2 = key('/w/dimagi/ddd/bednet/run-002')
    const narrative = key('/w/dimagi/ddd/bednet')
    expect(run1?.pageKey).not.toBe(run2?.pageKey)
    expect(run1?.pageKey).not.toBe(narrative?.pageKey)
    expect(narrative?.subLocation).toBe('Narrative')
    expect(run1?.subLocation).toBe('Run')
  })

  it('lands a global (non-tenant) page in the ~global sentinel namespace', () => {
    // The `~` prefix is a security boundary, not cosmetics: the server's
    // page-key parser only treats `~global` as "skip the membership gate",
    // and no client-assertable workspace slug can contain a `~`. A bare
    // `global` here would be a workspace name a user can create.
    expect(key('/review/abc')?.pageKey).toBe('canopy:~global:review:abc')
  })

  it('never emits a bare "global" workspace segment from any global rule', () => {
    expect(key('/review/abc')?.pageKey.startsWith('canopy:~global:')).toBe(true)
  })

  it('namespaces by app so two apps never collide on the same tenant route', () => {
    expect(pageKeyFor('canopy', '/w/dimagi/ddd/bednet', canopyPresenceRules)?.pageKey)
      .toBe('canopy:dimagi:ddd:bednet')
    expect(pageKeyFor('ace', '/w/dimagi/ddd/bednet', canopyPresenceRules)?.pageKey)
      .toBe('ace:dimagi:ddd:bednet')
  })
})
