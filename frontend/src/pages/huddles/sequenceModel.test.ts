import { describe, expect, it } from 'vitest'
import type { Huddle } from '@/api/huddles'
import type { AgentThread } from '@/api/threads'
import live from './__fixtures__/work-fleet-20261006.json'
import { agreed, threads } from './__fixtures__/agreementThreads'
import { arcsFor, outcomeOf } from './huddleModel'
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

describe('step 4 — settling changes in direct conversations', () => {
  const withThreads = { ...h, threads } as Huddle
  const steps = sequenceOf(withThreads)
  const four = steps.find((s) => s.step === 4)!

  it('adds "Step 4 · Settling changes" before the result', () => {
    expect(steps.map((s) => s.step)).toEqual([1, 2, 3, 4, 0])
    expect(four.title).toBe('Settling changes')
  })

  it('draws each message as a direct arrow from the speaker to the other agent', () => {
    const direct = four.messages.filter((m) => m.kind === 'direct')
    // In time order: the two conversations ran side by side.
    expect(direct.map((m) => [m.from, m.to])).toEqual([
      ['eva', ['echo']], ['echo', ['ace']], ['echo', ['eva']], ['ace', ['echo']],
    ])
    expect(direct.every((m) => !m.to.includes(h.leader))).toBe(true)
    expect(direct[0].label).toMatch(/^Suggests a change: A clip works for me/)
    expect(direct[1].label).toMatch(/^Asks: /)
    expect(direct[2].label).toMatch(/^Agrees: /)
  })

  it('ends each conversation with how it settled', () => {
    const ends = four.messages.filter((m) => m.kind === 'settled')
    expect(ends.map((m) => m.label)).toEqual([
      'Closed it: Eva and Echo — agreed on “IDM talk: live demo from Ace, story slide from Echo”',
      'Closed it: Echo and Ace — ran out of messages without agreeing on “Take PRIDE cholera story to reviewed draft”',
    ])
    // The moderator closes a thread, to both sides — not one of the agents.
    expect(ends.map((m) => [m.from, m.to])).toEqual([['ada', ['eva', 'echo']], ['ada', ['echo', 'ace']]])
  })

  it('still never goes back in time', () => {
    const times = steps.flatMap((s) => s.messages.map((m) => m.at)).filter(Boolean).map((t) => new Date(t as string).getTime())
    expect(times).toEqual([...times].sort((a, b) => a - b))
  })

  it('has no step 4 when no thread was opened', () => {
    expect(sequenceOf(h).some((s) => s.step === 4)).toBe(false)
  })
})

describe('agreement threads settle amends', () => {
  it('an agreed thread is a co-sign; a thread out of messages is not', () => {
    const withThreads = { ...h, threads } as Huddle
    const arcs = arcsFor(withThreads)
    const echoOnIdm = arcs.find((a) => a.partner === 'echo' && a.lead === 'eva' && a.title.startsWith('IDM talk'))!
    expect(echoOnIdm).toMatchObject({ state: 'amend-accepted', thread: 'thr-aaaaaaaaaaaa' })
    const aceOnPride = arcs.find((a) => a.partner === 'ace' && a.lead === 'echo')!
    expect(aceOnPride).toMatchObject({ state: 'amend-rejected', thread: 'thr-bbbbbbbbbbbb' })
    // Without threads they are still plain "in, with changes".
    expect(arcsFor(h).find((a) => a.key === echoOnIdm.key)!.state).toBe('amend')
  })

  it('an open thread links the arc but decides nothing yet', () => {
    const open = { ...agreed, status: 'open', outcome: {} } as AgentThread
    const arc = arcsFor({ ...h, threads: [open] } as Huddle).find((a) => a.partner === 'echo' && a.lead === 'eva')!
    expect(arc).toMatchObject({ state: 'amend', thread: agreed.id })
  })

  it('an idea whose only amend was agreed in a thread no longer waits on it', () => {
    const running = { ...h, finished: false, outputs: [] } as Huddle
    const idm = (x: Huddle) => Object.values(outcomeOf(x)).flat().find((p) => 'title' in p && String(p.title).startsWith('IDM talk')) as ReturnType<typeof outcomeOf>['open'][number]
    expect(idm(running)).toMatchObject({ verdict: 'open', hold: { kind: 'amend', who: ['echo'] } })
    expect(idm({ ...running, threads } as Huddle)).toMatchObject({ verdict: 'filed', hold: null })
  })
})
