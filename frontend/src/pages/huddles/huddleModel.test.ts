import { describe, expect, it } from 'vitest'
import type { HuddleCell } from '@/api/huddles'
import { arcsFor, cellState, columns, countdown, critiqueFrom, normAnswer, roundName, roundsToShow } from './huddleModel'

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
