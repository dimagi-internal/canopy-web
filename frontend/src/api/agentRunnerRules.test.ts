import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentRunnerRuleOut } from './agents'

const GET = vi.fn()
const PUT = vi.fn()
const DELETE = vi.fn()
vi.mock('./client.v2', () => ({ apiV2: { GET, PUT, DELETE }, WORKSPACE_HEADER: 'X-Canopy-Workspace' }))

const { saveAgentRunnerRules } = await import('./agents')

function row(over: Partial<AgentRunnerRuleOut>): AgentRunnerRuleOut {
  return {
    source: 'ace_web', actor: '', rank: 0, runner_id: 'ec2-1', runner_name: 'cloud-ec2-1',
    kind: 'cloud', strict: true, online: true, ready: true, enabled: true, queued_count: 0,
    turn_mode: '', ...over,
  }
}

// ACE on 2026-10-05: Matt's work on the cloud box, Sarvesh's on his own laptop.
const prev = [
  row({ actor: 'mtheis@dimagi.com' }),
  row({ actor: 'stewari@dimagi.com', runner_id: 'st-mbp', runner_name: 'st-mbp' }),
]
const asEdits = (rules: AgentRunnerRuleOut[]) =>
  rules.map((r) => ({ source: r.source, actor: r.actor, runnerIds: [r.runner_id], strict: r.strict, turnMode: '' as const }))

afterEach(() => vi.clearAllMocks())

describe('saveAgentRunnerRules', () => {
  it('sends only the rule that changed — never someone else\'s', async () => {
    PUT.mockResolvedValue({ data: [] })
    GET.mockResolvedValue({ data: prev })
    const next = asEdits(prev)
    next[0] = { ...next[0], runnerIds: ['ec2-2', 'ec2-1'] }

    await saveAgentRunnerRules('ace', prev, next)

    expect(PUT).toHaveBeenCalledTimes(1)
    const [path, opts] = PUT.mock.calls[0]
    expect(path).toBe('/api/agents/{slug}/runner-rules/{source}')
    expect(opts.params).toEqual({ path: { slug: 'ace', source: 'ace_web' }, query: { actor: 'mtheis@dimagi.com' } })
    expect(opts.body.runners.map((r: { runner_id: string }) => r.runner_id)).toEqual(['ec2-2', 'ec2-1'])
    expect(DELETE).not.toHaveBeenCalled()
  })

  it('sends nothing when nothing changed', async () => {
    GET.mockResolvedValue({ data: prev })
    await saveAgentRunnerRules('ace', prev, asEdits(prev))
    expect(PUT).not.toHaveBeenCalled()
    expect(DELETE).not.toHaveBeenCalled()
  })

  it('deletes a rule the edit dropped, and only that one', async () => {
    DELETE.mockResolvedValue({})
    GET.mockResolvedValue({ data: [prev[1]] })
    await saveAgentRunnerRules('ace', prev, asEdits([prev[1]]))
    expect(DELETE).toHaveBeenCalledTimes(1)
    expect(DELETE.mock.calls[0][1].params).toEqual({
      path: { slug: 'ace', source: 'ace_web' }, query: { actor: 'mtheis@dimagi.com' },
    })
    expect(PUT).not.toHaveBeenCalled()
  })

  it('keeps a disabled runner disabled when its rule is edited', async () => {
    const withOff = [row({ actor: 'mtheis@dimagi.com', enabled: false })]
    PUT.mockResolvedValue({ data: [] })
    GET.mockResolvedValue({ data: withOff })
    await saveAgentRunnerRules('ace', withOff, [{
      source: 'ace_web', actor: 'mtheis@dimagi.com', runnerIds: ['ec2-1', 'ec2-2'], strict: true, turnMode: '',
    }])
    expect(PUT.mock.calls[0][1].body.runners).toEqual([
      { runner_id: 'ec2-1', enabled: false },
      { runner_id: 'ec2-2', enabled: true },
    ])
  })

  it('surfaces a refusal', async () => {
    PUT.mockResolvedValue({ error: { detail: "you don't administer runner(s) st-mbp" } })
    const next = asEdits(prev)
    next[0] = { ...next[0], runnerIds: ['st-mbp'] }
    await expect(saveAgentRunnerRules('ace', prev, next)).rejects.toThrow(/st-mbp/)
  })
})
