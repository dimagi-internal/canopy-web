import { describe, expect, it } from 'vitest'
import type { Huddle, HuddleCell } from '@/api/huddles'
import {
  arcsFor, cellState, columns, countdown, critiqueFrom, holdWords, normAnswer, outcomeOf, roundName, roundsToShow,
  sizeWords, taskProposalKey, taskStatusWords, type ProposalInfo,
} from './huddleModel'

function cell(member: string, round: number, block: Record<string, unknown> | null, over: Partial<HuddleCell> = {}): HuddleCell {
  return {
    member, round, attempt: 1, turn_id: `${member}-${round}`, status: 'done', created_at: null,
    finished_at: null, content_hidden: false, prompt: '', block, reply_source: block ? 'closeout' : 'none',
    reply_error: '', has_transcript: false, ...over,
  }
}

const proposal = (title: string, lead: string, partners: string[]) => ({ title, lead, with: partners })

describe('arcsFor', () => {
  it('draws one arc per partner, coloured by its round-3 answer, pending until it answers', () => {
    const cells = [
      cell('eva', 2, { proposals: [proposal('Q4 brief', 'eva', ['echo', 'hal']), proposal('Solo', 'eva', [])] }),
      cell('ace', 2, { proposals: [proposal('Fix CI', 'ace', ['hal'])] }),
      cell('echo', 3, { answers: [{ title: 'q4 brief', lead: 'eva', answer: 'Co-sign' }] }),
      cell('hal', 3, { answers: [{ title: 'Fix CI', lead: 'ace', answer: 'amend', note: 'smaller' }] }),
    ]
    const arcs = arcsFor({ cells })
    const by = Object.fromEntries(arcs.map((a) => [`${a.partner}->${a.lead}:${a.title}`, a]))
    expect(by['echo->eva:Q4 brief']).toMatchObject({ state: 'co-sign', from: 'echo-3', to: 'eva-2' })
    expect(by['hal->eva:Q4 brief']).toMatchObject({ state: 'pending', from: 'hal-3', to: 'eva-2' })
    expect(by['hal->ace:Fix CI']).toMatchObject({ state: 'amend', note: 'smaller' })
    expect(arcs).toHaveLength(3)
  })

  it('anchors a partner with no round-3 cell on its column head', () => {
    const arcs = arcsFor({ cells: [cell('eva', 2, { proposals: [proposal('A', 'eva', ['echo'])] })] })
    expect(arcs[0]).toMatchObject({ from: 'head-echo', to: 'eva-2', state: 'pending' })
  })

  it("turns an amend the lead accepted in round 4 into a co-sign, a rejected one into held", () => {
    const cells = [
      cell('eva', 2, { proposals: [proposal('Pipeline sheet', 'eva', ['hal', 'echo']), proposal('Funder map', 'eva', ['hal'])] }),
      cell('hal', 3, { answers: [
        { title: 'Pipeline sheet', lead: 'eva', answer: 'amend', note: 'weekly' },
        { title: 'Funder map', lead: 'eva', answer: 'amend', note: 'smaller' },
      ] }),
      cell('echo', 3, { answers: [{ title: 'Pipeline sheet', lead: 'eva', answer: 'co-sign' }] }),
    ]
    const before = arcsFor({ cells })
    expect(before.find((a) => a.partner === 'hal' && a.title === 'Pipeline sheet')?.state).toBe('amend')

    const after = arcsFor({ cells: [...cells, cell('eva', 4, { resolutions: [
      { title: 'pipeline sheet', lead: 'eva', resolution: 'accept', note: 'fair', proposal: proposal('Pipeline sheet', 'eva', ['hal', 'echo']) },
      { title: 'Funder map', lead: 'eva', resolution: 'reject', note: 'needs daily' },
    ] })] })
    const by = Object.fromEntries(after.map((a) => [`${a.partner}:${a.title}`, a.state]))
    expect(by['hal:Pipeline sheet']).toBe('amend-accepted')
    expect(by['hal:Funder map']).toBe('amend-rejected')
    // A partner who co-signed outright is untouched by the resolution.
    expect(by['echo:Pipeline sheet']).toBe('co-sign')
  })

  it('ignores a round-4 resolution from someone who is not the lead', () => {
    const cells = [
      cell('eva', 2, { proposals: [proposal('A', 'eva', ['hal'])] }),
      cell('hal', 3, { answers: [{ title: 'A', lead: 'eva', answer: 'amend' }] }),
      cell('echo', 4, { resolutions: [{ title: 'A', lead: 'eva', resolution: 'accept' }] }),
    ]
    expect(arcsFor({ cells })[0].state).toBe('amend')
  })

  it('still draws an answer whose proposal it cannot see, to the lead column', () => {
    const arcs = arcsFor({ cells: [cell('echo', 3, { answers: [{ title: 'A', lead: 'eva', answer: 'decline' }] })] })
    expect(arcs[0]).toMatchObject({ from: 'echo-3', to: 'head-eva', state: 'decline' })
  })
})

it('normalises answers', () => {
  expect(normAnswer('co-sign')).toBe('co-sign')
  expect(normAnswer('cosigned')).toBe('co-sign')
  expect(normAnswer('Amend: smaller')).toBe('amend')
  expect(normAnswer('DECLINE')).toBe('decline')
  expect(normAnswer(undefined)).toBe('pending')
})

it('names rounds by type and always shows the type rounds', () => {
  expect(roundName('work', 2)).toBe('Roundtable')
  expect(roundName('health', 2)).toBe('Round 2')
  expect(roundName('work', 4)).toBe('Resolve')
  expect(roundsToShow({ type: 'work', rounds_dispatched: 1, cells: [] })).toEqual([1, 2, 3])
  // Round 4 (resolve) is conditional: shown only once it is dispatched.
  expect(roundsToShow({ type: 'work', rounds_dispatched: 3, cells: [] })).toEqual([1, 2, 3])
  expect(roundsToShow({ type: 'work', rounds_dispatched: 4, cells: [] })).toEqual([1, 2, 3, 4])
  expect(roundsToShow({ type: 'x', rounds_dispatched: 2, cells: [] })).toEqual([1, 2])
})

it('orders columns by the member list, then late arrivals', () => {
  expect(columns({ members: ['eva', 'echo'], cells: [cell('hal', 1, null), cell('eva', 1, null)] })).toEqual(['eva', 'echo', 'hal'])
})

it('classifies cells', () => {
  expect(cellState(cell('a', 1, { x: 1 }))).toBe('replied')
  expect(cellState(cell('a', 1, null, { status: 'running' }))).toBe('waiting')
  expect(cellState(cell('a', 1, null, { status: 'failed' }))).toBe('failed')
  expect(cellState(cell('a', 1, null))).toBe('no-reply')
  expect(cellState(cell('a', 1, null, { content_hidden: true }))).toBe('hidden')
})

it('pulls the leader critique out of a round prompt', () => {
  const p = "Huddle h — round 2.\n\nada's questions for you:\nWhy now? Who else needs it?\n\nPropose AT MOST 3"
  expect(critiqueFrom(p, 'ada')).toBe('Why now? Who else needs it?')
  expect(critiqueFrom("ada's questions for you:\nnone\n\nPropose", 'ada')).toBe('')
  expect(critiqueFrom('no critique here', 'ada')).toBe('')
})

it('counts down to a deadline', () => {
  const now = new Date('2026-10-06T10:00:00Z')
  expect(countdown('2026-10-06T10:42:00Z', now)).toBe('42m')
  expect(countdown('2026-10-06T11:05:00Z', now)).toBe('1h 05m')
  expect(countdown('2026-10-06T09:00:00Z', now)).toBe('')
  expect(countdown(null, now)).toBe('')
})

// ── the outcome ──────────────────────────────────────────────────────────────

describe('outcomeOf — the live work-fleet-20261006 huddle', async () => {
  const { default: live } = await import('./__fixtures__/work-fleet-20261006.json')
  const h = live as unknown as Huddle
  const o = outcomeOf(h)

  it('files the three proposals the email named, held the two it held', () => {
    expect(o.filed.map((p) => p.title)).toEqual([
      'Pre-flight one live demo for the IDM talk',
      'Diagnose chrome-sales MCP connect failures on cloud-ec2-2',
      'Deploy connect-labs ALB 5xx alarms, then bound web tier',
    ])
    expect(o.held.map((p) => p.title).sort()).toEqual([
      'IDM talk: live demo from Ace, story slide from Echo',
      'Take PRIDE cholera story to reviewed draft',
    ])
    expect(o.open).toEqual([])
  })

  it('groups the five board tasks under their proposals — one per agent on a joint one', () => {
    expect(o.unmatched).toEqual([])
    const tasks = Object.fromEntries(o.filed.map((p) => [p.title, p.tasks.map((t) => `${t.agent}:${t.ext_id}`)]))
    expect(tasks).toEqual({
      'Pre-flight one live demo for the IDM talk': ['ace:T9', 'eva:T45'],
      'Diagnose chrome-sales MCP connect failures on cloud-ec2-2': ['hal:T50', 'eva:T46'],
      'Deploy connect-labs ALB 5xx alarms, then bound web tier': ['hal:T51'],
    })
  })

  it('says why each held one is held, naming who amended', () => {
    const idm = o.held.find((p) => p.lead === 'eva')!
    expect(idm.hold).toEqual({ kind: 'amend', who: ['echo'] })
    expect(holdWords(idm, 'ada').why).toBe("echo co-signed with conditions; eva (the lead) hasn't resolved them.")
    expect(holdWords(idm, 'ada').clear).toMatch(/round 4 would/)
    const pride = o.held.find((p) => p.lead === 'echo')!
    expect(pride.hold).toMatchObject({ kind: 'amend', who: expect.arrayContaining(['eva', 'ace']) })
  })

  it('keeps the goal a proposal serves whole', () => {
    expect(o.filed[0].priority).toBe(
      'Gates/IDM talk Tue Oct 13 11:45am PT + Seattle blitz 10/12-14 and the deck due 10/9 (T40/T27; goals sheet Oct set, board, trip-planning state)')
  })
})

describe('outcomeOf — round 4', () => {
  const cells = [
    cell('eva', 2, { proposals: [proposal('Pipeline sheet', 'eva', ['hal']), proposal('Funder map', 'eva', ['hal'])] }),
    cell('hal', 3, { answers: [
      { title: 'Pipeline sheet', lead: 'eva', answer: 'amend', note: 'weekly' },
      { title: 'Funder map', lead: 'eva', answer: 'amend', note: 'smaller' },
    ] }),
    cell('eva', 4, { resolutions: [
      { title: 'Pipeline sheet', lead: 'eva', resolution: 'accept' },
      { title: 'Funder map', lead: 'eva', resolution: 'reject' },
    ] }),
  ]
  const task = { agent: 'eva', task_id: 1, ext_id: 'T1', title: 'Pipeline sheet', status: 'suggested', assigned: 'eva', project: '', url: '/b' }

  it('treats an accepted amend as filed and a rejected one as held', () => {
    const o = outcomeOf({ cells, members: ['eva', 'hal'], outputs: [task], finished: true })
    expect(o.filed.map((p) => p.title)).toEqual(['Pipeline sheet'])
    expect(o.held.map((p) => [p.title, p.hold?.kind])).toEqual([['Funder map', 'amend-rejected']])
  })

  it('before filing, an accepted amend is agreed and an open one is still being decided', () => {
    const o = outcomeOf({ cells: cells.slice(0, 2), members: ['eva', 'hal'], outputs: [], finished: false })
    expect(o.filed).toEqual([])
    expect(o.open.map((p) => p.hold?.kind)).toEqual(['amend', 'amend'])
    const after = outcomeOf({ cells, members: ['eva', 'hal'], outputs: [], finished: false })
    expect(after.filed.map((p) => p.title)).toEqual(['Pipeline sheet'])
    expect(after.filed[0].tasks).toEqual([])
  })

  it('holds a fully co-signed proposal the leader did not file, as a filing gate', () => {
    const o = outcomeOf({
      cells: [cell('eva', 2, { proposals: [proposal('Solo', 'eva', [])] })], members: ['eva'], outputs: [], finished: true,
    })
    expect(o.held[0].hold?.kind).toBe('gate')
  })
})

describe('taskProposalKey', () => {
  const ps = [
    { key: 'ace|demo', title: 'Demo', lead: 'ace' },
    { key: 'eva|demo', title: 'Demo', lead: 'eva' },
    { key: 'eva|demo plus', title: 'Demo plus', lead: 'eva' },
  ] as ProposalInfo[]
  it("matches a partner's part by title and lead, the lead's task by title and agent", () => {
    expect(taskProposalKey({ title: "Demo — hal's part (lead eva)", agent: 'hal' }, ps)).toBe('eva|demo')
    expect(taskProposalKey({ title: 'Demo', agent: 'ace' }, ps)).toBe('ace|demo')
    expect(taskProposalKey({ title: 'Demo plus', agent: 'eva' }, ps)).toBe('eva|demo plus')
    expect(taskProposalKey({ title: 'Something else', agent: 'eva' }, ps)).toBeNull()
  })
})

describe('plain words', () => {
  it('spells out size, confidence and task status', () => {
    expect(sizeWords('S', 0.6)).toBe('small · 60% confident')
    expect(sizeWords('M', null)).toBe('medium')
    expect(taskStatusWords({ status: 'suggested', assigned: 'eva' })).toBe('awaiting your accept / decline')
    expect(taskStatusWords({ status: 'in_progress', assigned: 'eva' })).toBe('in progress · waiting on eva')
  })
})
