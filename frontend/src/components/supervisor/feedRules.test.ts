import { describe, expect, it } from 'vitest'
import type { ChatSession } from '@/api/chat'
import { feedSessions, feedSources, parkedByRunner } from './feedRules'

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

describe('parkedByRunner', () => {
  const fleet = [
    { id: 'r1', name: 'box', status_note: 'heartbeat lost', ready_note: 'cdp down', paused_note: 'gone fishing' },
    { id: 'r2', name: 'other', status_note: '', ready_note: 'emdash CDP unreachable', paused_note: '' },
  ]
  it('groups parked sessions by runner, busiest first, with the runner\'s own note', () => {
    const groups = parkedByRunner(
      [
        s('live'),
        s('a', { feed_status: 'parked', runner_name: 'other', runner_status: 'degraded' }),
        s('b', { feed_status: 'parked', runner_name: 'box', runner_status: 'stale' }),
        s('c', { feed_status: 'parked', runner_name: 'other', runner_status: 'degraded', waiting_on_you: true }),
      ],
      fleet,
    )
    expect(groups.map((g) => [g.runnerName, g.count, g.waiting, g.reason, g.note, g.runner?.id])).toEqual([
      ['other', 2, 1, 'offline', 'emdash CDP unreachable', 'r2'],
      ['box', 1, 0, 'offline', 'heartbeat lost', 'r1'],
    ])
  })

  it('reads the pause note for a paused runner, and survives a runner the fleet does not list', () => {
    const groups = parkedByRunner(
      [
        s('a', { feed_status: 'parked', runner_name: 'box', runner_status: 'paused' }),
        s('b', { feed_status: 'parked', runner_name: 'retired-box', runner_status: 'stale' }),
      ],
      fleet,
    )
    expect(groups.find((g) => g.runnerName === 'box')).toMatchObject({ reason: 'paused', note: 'gone fishing' })
    expect(groups.find((g) => g.runnerName === 'retired-box')).toMatchObject({ runner: null, note: '' })
  })
})
