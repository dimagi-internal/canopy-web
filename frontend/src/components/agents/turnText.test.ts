import { describe, expect, it } from 'vitest'
import type { AgentTurnOut } from '@/api/agents'
import { promptHasMore, turnBody, turnDuration, turnHeadline, turnTrigger } from './turnText'

// The shape labs actually serves for most turns: dispatched, ran, never reported.
function unreported(over: Partial<AgentTurnOut> = {}): AgentTurnOut {
  return {
    id: 'db1d471f-b60e-4c62-a4a3-45da31c7f4f3',
    agent_slug: 'ace',
    cli_session_id: '',
    title: '',
    summary: '',
    task_ext_ids: [],
    work_product_urls: [],
    session_slug: '',
    share_token: '',
    started_at: '2026-10-01T18:06:15Z',
    ended_at: '2026-10-01T18:06:21Z',
    source: '',
    created_at: '2026-10-01T18:06:12Z',
    status: 'done',
    origin: 'api',
    emdash_task_id: 'c-fix-brief-61bb',
    reported_at: null,
    prompt: '',
    result_note: '',
    origin_ref: {},
    content_hidden: false,
    ...over,
  }
}

describe('turnHeadline', () => {
  it('prefers the reported title', () => {
    expect(turnHeadline(unreported({ title: 'Shipped X', prompt: 'do X' }))).toBe('Shipped X')
  })
  it('falls back to the first prompt line, markdown markers stripped', () => {
    expect(turnHeadline(unreported({ prompt: '\n## Fix the brief\nmore detail' }))).toBe('Fix the brief')
  })
  it('truncates a long first line', () => {
    const h = turnHeadline(unreported({ prompt: 'a'.repeat(300) }))
    expect(h).toHaveLength(140)
    expect(h.endsWith('…')).toBe(true)
  })
  it('names the trigger when there is no prompt either', () => {
    expect(turnHeadline(unreported({ origin: 'canopy_scheduler', origin_ref: { slot: 'daily' } })))
      .toBe('schedule · daily turn')
  })
})

describe('turnBody', () => {
  it('prefers the reported summary, else the result note', () => {
    expect(turnBody(unreported({ summary: 'S', result_note: 'R' }))).toBe('S')
    expect(turnBody(unreported({ result_note: 'R' }))).toBe('R')
    expect(turnBody(unreported())).toBe('')
  })
})

describe('turnTrigger', () => {
  it('labels a manual scheduler fire', () => {
    expect(turnTrigger(unreported({ origin: 'canopy_scheduler', origin_ref: { manual: true } })))
      .toBe('schedule · run now')
  })
  it('passes other origins through', () => {
    expect(turnTrigger(unreported({ origin: 'email' }))).toBe('email')
  })
})

describe('turnDuration', () => {
  it('formats seconds, minutes and hours', () => {
    expect(turnDuration(unreported())).toBe('6s')
    expect(turnDuration(unreported({ ended_at: '2026-10-01T18:16:15Z' }))).toBe('10m')
    expect(turnDuration(unreported({ ended_at: '2026-10-01T20:36:15Z' }))).toBe('2h 30m')
  })
  it('is empty while a turn has not finished', () => {
    expect(turnDuration(unreported({ ended_at: null }))).toBe('')
  })
})

describe('promptHasMore', () => {
  it('is false when the headline already is the whole prompt', () => {
    expect(promptHasMore(unreported({ prompt: 'just this' }))).toBe(false)
    expect(promptHasMore(unreported({ prompt: 'line one\nline two' }))).toBe(true)
  })
})
