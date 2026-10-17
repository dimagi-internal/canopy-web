import { describe, expect, it } from 'vitest'

import type { ChatSession } from '@/api/chat'
import { REPLY_TAIL, toCandidate } from './useCloseReview'

const base = {
  id: 's1', title: '  Ship it ', status: 'active', running: false, waiting_on_you: false,
  last_activity_at: '2026-10-16T00:00:00Z',
} as unknown as ChatSession

describe('what /ide hands the agent for a close review', () => {
  it('carries the rail’s facts, with the emdash project over the bare project', () => {
    const c = toCandidate({ ...base, emdash_project: 'canopy-web', project: '', runner_name: 'cloud-ec2-2',
      runner_status: 'online', turn_mode: 'auto', agent_slug: 'ace', agent_spoke_last: true,
      last_reply: 'Done. Safe to close.' } as ChatSession)
    expect(c).toMatchObject({ id: 's1', title: 'Ship it', project: 'canopy-web', runner: 'cloud-ec2-2',
      runner_status: 'online', mode: 'auto', agent: 'ace', agent_spoke_last: true,
      last_reply: 'Done. Safe to close.' })
  })

  it('keeps the END of a long reply, where "done" is said', () => {
    const c = toCandidate({ ...base, last_reply: 'x'.repeat(5000) + ' ALL DONE' } as ChatSession)
    expect(c.last_reply.length).toBe(REPLY_TAIL + 1)
    expect(c.last_reply.startsWith('…')).toBe(true)
    expect(c.last_reply.endsWith('ALL DONE')).toBe(true)
  })

  it('names an untitled, unbound session plainly', () => {
    expect(toCandidate({ ...base, title: '' } as ChatSession)).toMatchObject({ title: '(untitled)', runner: '', mode: '' })
  })
})
