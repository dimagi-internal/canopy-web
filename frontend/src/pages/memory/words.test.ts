import { describe, expect, it } from 'vitest'

import type { HcpAuditEvent, HcpGrant, PersonFactDetail } from '../../api/people'
import { activeAccess, durationOf, eventSentence, groupByPlace, placeOf, reads, readsByEntry } from './words'

const grant = (over: Partial<HcpGrant> & { channel?: string; agent?: string | null } = {}): HcpGrant => ({
  grantId: over.grantId ?? 'g1',
  client: over.client ?? { id: 'c1', name: 'ACE' },
  scopes: over.scopes ?? ['hcp:read:general_preferences:x'],
  restrictions: [],
  grantType: over.grantType ?? 'persistent',
  grantor: null,
  issuedAt: '2026-10-13T10:00:00Z',
  expiresAt: over.expiresAt ?? null,
  status: over.status ?? 'active',
  canopy: { agent: over.agent === undefined ? 'ace' : over.agent, channel: over.channel ?? 'connect', host: '', workspace: null, modality: 'chat' },
})

const event = (over: Partial<HcpAuditEvent>): HcpAuditEvent => ({
  eventId: 'e',
  eventType: 'preference.read',
  timestamp: '2026-10-16T10:00:00Z',
  actorId: 'agent:ace/model:claude',
  actorType: 'agent',
  entryId: 'urn:uuid:1',
  category: 'work_context',
  purpose: null,
  detail: null,
  ...over,
})

describe('memory words', () => {
  it('names a channel without its raw id', () => {
    expect(placeOf(grant({ channel: 'slack:t02qg622c' }))).toBe('Slack')
    expect(placeOf(grant({ channel: 'app_view:connect-labs' }))).toBe('the connect-labs app')
  })

  it('folds an agent’s active grants into one card and drops past ones', () => {
    const rows = activeAccess([
      grant({ grantId: 'a', client: { id: 'c', name: 'ACE' } }),
      grant({ grantId: 'b', client: { id: 'c', name: 'ACE over Slack:t02' }, channel: 'slack:t02', grantType: 'temporary' }),
      grant({ grantId: 'c', status: 'revoked' }),
    ])
    expect(rows).toHaveLength(1)
    expect(rows[0].name).toBe('ACE')
    expect(rows[0].grants.map((g) => g.grantId)).toEqual(['a', 'b'])
    expect(rows[0].places).toEqual(['canopy', 'Slack'])
    expect(rows[0].kept).toBe(true)
  })

  it('says a conversation grant ends, once', () => {
    const now = new Date('2026-10-16T10:00:00Z')
    expect(durationOf(grant({ grantType: 'temporary', expiresAt: '2026-10-16T15:00:00Z' }), now)).toBe(
      'For one conversation · ends in 5 h',
    )
  })

  it('groups entries by organisation and workspace, and asks canopy who reads each', () => {
    const fact = (id: number, workspace: string | null, org?: string) =>
      ({ id, workspace, workspace_name: workspace ?? '', org: org ?? null, org_name: org ? 'Dimagi' : '' }) as unknown as PersonFactDetail
    const { orgs, personal } = groupByPlace([fact(1, 'connect', 'dimagi'), fact(2, 'ace', 'dimagi'), fact(3, null), fact(4, 'connect', 'dimagi')])
    expect(orgs.map((o) => [o.name, o.places.map((p) => [p.workspace, p.facts.length])])).toEqual([
      ['Dimagi', [['connect', 2], ['ace', 1]]],
    ])
    expect(personal.facts.map((f) => f.id)).toEqual([3])
    const [ace] = activeAccess([{ ...grant(), canopy: { ...grant().canopy, reaches: ['ace', 'connect', 'dimagi'] } }])
    expect([reads(ace, 'connect'), reads(ace, null)]).toEqual([true, false])
  })

  it('turns audit events into sentences and counts reads per entry', () => {
    const names = new Map([['ace', 'ACE']])
    expect(eventSentence(event({ purpose: 'draft a reply' }), names)).toEqual({
      who: 'ACE',
      did: 'read your memory — “My work and role”',
      why: 'draft a reply',
    })
    expect(
      eventSentence(event({ eventType: 'grant.expired', actorType: 'system', actorId: 'canopy', detail: 'ACE over Slack:t02: the session ended' }), names).who,
    ).toBe('ACE')
    expect(eventSentence(event({ eventType: 'grant.issued', actorType: 'user', detail: 'client=Hal; key=k' }), names)).toMatchObject({
      who: 'Hal',
      did: 'was allowed to read your memory — by you',
    })
    const reads = readsByEntry(
      [event({}), event({ timestamp: '2026-10-17T10:00:00Z' }), event({ actorType: 'user', actorId: 'jj@x' })],
      names,
    )
    expect(reads.get('urn:uuid:1')).toEqual({ count: 2, last: '2026-10-17T10:00:00Z', by: ['ACE'] })
  })
})
