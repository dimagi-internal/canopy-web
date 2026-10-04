import { describe, expect, it } from 'vitest'
import type { ChatSession } from '@/api/chat'
import { feedSessions, feedSources, needsNextPrompt } from './feedRules'

const s = (id: string, fields: Partial<ChatSession> = {}): ChatSession =>
  ({
    id,
    title: id,
    running: false,
    waiting_on_you: false,
    agent_spoke_last: false,
    last_reply: '',
    last_activity_at: '2026-10-03T10:00:00Z',
    runner_online: null,
    runner_status: null,
    ...fields,
  }) as unknown as ChatSession

describe('needsNextPrompt', () => {
  it('a finished turn — the agent spoke last and stopped — is your turn', () => {
    expect(needsNextPrompt(s('a', { agent_spoke_last: true }))).toBe(true)
  })

  it('a running session is mid-turn, even when its last row is the agent', () => {
    expect(needsNextPrompt(s('a', { agent_spoke_last: true, running: true }))).toBe(false)
  })

  it('a session you already replied to is not waiting on you', () => {
    expect(needsNextPrompt(s('a', { agent_spoke_last: false }))).toBe(false)
  })

  it('a session blocked on a dialog is waiting on you, running or not', () => {
    expect(needsNextPrompt(s('a', { waiting_on_you: true, running: true }))).toBe(true)
  })
})

describe('feedSessions', () => {
  it('is a queue: oldest first, dialogs included, new arrivals last', () => {
    const { feed } = feedSessions([
      s('old', { agent_spoke_last: true, last_activity_at: '2026-10-01T00:00:00Z' }),
      s('new', { agent_spoke_last: true, last_activity_at: '2026-10-03T00:00:00Z' }),
      s('dialog', { waiting_on_you: true, last_activity_at: '2026-10-04T00:00:00Z' }),
      s('busy', { agent_spoke_last: true, running: true }),
    ])
    expect(feed.map((x) => x.id)).toEqual(['old', 'new', 'dialog'])
  })

  it('holds back sessions on an offline runner, and counts them', () => {
    const { feed, parked } = feedSessions([
      s('live', { agent_spoke_last: true, runner_online: true }),
      s('dead', { agent_spoke_last: true, runner_online: false, runner_status: 'stale' }),
    ])
    expect(feed.map((x) => x.id)).toEqual(['live'])
    expect(parked).toBe(1)
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
