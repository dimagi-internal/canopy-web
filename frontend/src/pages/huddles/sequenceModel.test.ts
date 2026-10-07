import { describe, expect, it } from 'vitest'
import type { Huddle } from '@/api/huddles'
import live from './__fixtures__/work-fleet-20261006.json'
import { lanesOf, sequenceOf } from './sequenceModel'

const h = live as unknown as Huddle

describe('sequenceOf — the huddle as messages in time order', () => {
  const steps = sequenceOf(h)

  it('runs ask → answers per step, then the result to you', () => {
    expect(steps.map((s) => s.step)).toEqual([1, 2, 3, 0])
    for (const s of steps.slice(0, 3)) {
      expect(s.messages[0]).toMatchObject({ kind: 'ask', from: 'ada' })
      expect(s.messages.slice(1).map((m) => m.kind)).toEqual(['reply', 'reply', 'reply', 'reply'])
    }
    expect(steps[3].messages[0]).toMatchObject({ kind: 'result', from: 'ada', to: ['you'] })
    expect(lanesOf(h)).toEqual(['you', 'ada', 'ace', 'echo', 'eva', 'hal'])
  })

  it('orders answers by when they arrived, and never goes back in time', () => {
    const times = steps.flatMap((s) => s.messages.map((m) => m.at)).filter(Boolean).map((t) => new Date(t as string).getTime())
    expect(times).toEqual([...times].sort((a, b) => a - b))
  })

  it('says what each message did, in one plain line', () => {
    const msg = (k: string) => steps.flatMap((s) => s.messages).find((m) => m.key === k)!
    expect(msg('ask-1').label).toBe('To everyone: What are you working on?')
    expect(msg('ask-2').label).toMatch(/^To everyone: Any ideas, alone or together\? — plus \d+ questions of her own$/)
    expect(msg('reply-hal-2').label).toMatch(/^Idea: “Deploy connect-labs ALB 5xx alarms.*” on its own$/)
    expect(msg('reply-eva-2').label).toMatch(/^2 ideas: /)
    expect(msg('reply-hal-3').chips).toEqual([expect.objectContaining({ answer: 'amend' })])
  })
})
