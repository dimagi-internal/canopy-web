import { describe, expect, it } from 'vitest'
import type { ChatSession } from '@/api/chat'
import { feedSessions, feedSources } from './feedRules'

const s = (id: string, fields: Partial<ChatSession> = {}): ChatSession =>
  ({
    id,
    title: id,
    feed_status: 'waiting',
    last_reply: '',
    last_activity_at: '2026-10-03T10:00:00Z',
    ...fields,
  }) as unknown as ChatSession

// WHICH sessions are on the feed is the server's call (apps/canopy_sessions/feed.py,
// pinned in tests/test_supervisor_feed.py); this only sorts and counts its verdict.
describe('feedSessions', () => {
  it('is a queue of the waiting ones: oldest first, new arrivals last', () => {
    const { feed } = feedSessions([
      s('old', { last_activity_at: '2026-10-01T00:00:00Z' }),
      s('new', { last_activity_at: '2026-10-03T00:00:00Z' }),
      s('dialog', { last_activity_at: '2026-10-04T00:00:00Z' }),
      s('busy', { feed_status: '' }),
    ])
    expect(feed.map((x) => x.id)).toEqual(['old', 'new', 'dialog'])
  })

  it('holds back parked sessions and counts them', () => {
    const { feed, parked } = feedSessions([s('live'), s('dead', { feed_status: 'parked' })])
    expect(feed.map((x) => x.id)).toEqual(['live'])
    expect(parked).toBe(1)
  })

  it("drops someone else's runner sessions without counting them", () => {
    const { feed, parked, auto } = feedSessions([s('mine'), s('sarvesh', { feed_status: 'not_yours' })])
    expect(feed.map((x) => x.id)).toEqual(['mine'])
    expect([parked, auto]).toEqual([0, 0])
  })

  it("holds an agent's own runs back unless asked, and counts them", () => {
    const rows = [s('chat'), s('cron', { feed_status: 'auto' })]
    expect(feedSessions(rows).feed.map((x) => x.id)).toEqual(['chat'])
    expect(feedSessions(rows).auto).toBe(1)
    expect(feedSessions(rows, { showAuto: true }).feed.map((x) => x.id)).toEqual(['chat', 'cron'])
  })
})

describe('feedSources', () => {
  it('groups by agent, or by project for an agentless chat, busiest first', () => {
    const src = feedSources([
      s('a', { agent_slug: 'hal' }),
      s('b', { agent_slug: null, project: 'canopy-web' }),
      s('c', { agent_slug: 'hal' }),
    ])
    expect(src.map((x) => [x.key, x.count])).toEqual([
      ['agent:hal', 2],
      ['project:canopy-web', 1],
    ])
  })
})
