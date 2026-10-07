import { describe, expect, it } from 'vitest'
import live from './__fixtures__/work-fleet-20261006.json'
import { askPath, flowArc, replyPath, rowAnchor } from './flowGeometry'
import {
  anchorKey, answerLines, arcsFor, critiqueAnswered, leaderAsks, proposalLines, reportSummary, resolutionLines, roundAsk,
  type Block,
} from './huddleModel'
import type { Huddle } from '@/api/huddles'

const h = live as unknown as Huddle
const cell = (m: string, r: number) => h.cells.find((c) => c.member === m && c.round === r)!

describe('leaderAsks — the questions the leader sent, out of the real prompts', () => {
  it('round 2: the bullets under "ada\'s questions for you:"', () => {
    const asks = leaderAsks(cell('eva', 2).prompt, 'ada')
    expect(asks).toHaveLength(3)
    expect(asks[0]).toEqual({ about: '', text: expect.stringMatching(/^You are the only member that read Jonathan's goals/) })
    expect(asks[2].text).toMatch(/blocked on humans/)
  })

  it('round 3: one critique per joint proposal, then the questions on your own proposals', () => {
    const asks = leaderAsks(cell('eva', 3).prompt, 'ada')
    expect(asks.map((a) => a.about)).toEqual([
      'Pre-flight one live demo for the IDM talk',
      'Take PRIDE cholera story to reviewed draft',
      'Diagnose chrome-sales MCP connect failures on cloud-ec2-2',
      'your own proposals',
    ])
    expect(asks[0].text).toMatch(/^This is the same work as Eva's/)
  })

  it('round 1 has none; prose and "none" are handled', () => {
    expect(leaderAsks(cell('eva', 1).prompt, 'ada')).toEqual([])
    expect(leaderAsks("ada's questions for you:\nnone\n\nPropose", 'ada')).toEqual([])
    expect(leaderAsks("ada's questions for you:\nIs Q4 realistic?\n\nPropose", 'ada')).toEqual([{ about: '', text: 'Is Q4 realistic?' }])
    expect(leaderAsks('', 'ada')).toEqual([])
  })

  it("names each round's ask", () => {
    expect(roundAsk('work', 1)).toBe('what are you working on?')
    expect(roundAsk('work', 4)).toBe('agree to the changes?')
    expect(roundAsk('other', 2)).toBe('step 2')
  })
})

describe('compact summaries', () => {
  it('round 1: counts and the top priority', () => {
    const s = reportSummary(cell('ace', 1).block as Block)
    expect(s.stats).toBe('5 worked on · 5 priorities · 4 needs')
    expect(s.top).toMatch(/^Ship demos\/dashboards/)
    expect(reportSummary({ priorities: ['x'], needs: ['y'] }).stats).toBe('1 priority · 1 need')
  })

  it('round 2: one line per proposal, partners without the lead, anchored', () => {
    const lines = proposalLines(cell('eva', 2).block as Block, 'eva')
    expect(lines.map((l) => [l.lead, l.partners])).toEqual([['eva', ['ace', 'echo']], ['hal', ['eva']]])
    expect(lines[1].anchor).toBe(anchorKey.proposal('hal', 'Diagnose chrome-sales MCP connect failures on cloud-ec2-2'))
    expect(critiqueAnswered(cell('eva', 2).block as Block, 'ada')).toBe("answered 3 of Ada's questions")
  })

  it('round 3: one row per answer; round 4: verdict + title', () => {
    const rows = answerLines(cell('eva', 3).block as Block, 'eva')
    expect(rows.map((r) => r.answer)).toEqual(['co-sign', 'amend', 'co-sign'])
    expect(rows[0].anchor).toBe(anchorKey.answer('eva', 'Pre-flight one live demo for the IDM talk'))
    expect(resolutionLines({ resolutions: [{ title: 'T', resolution: 'Accept' }] })).toEqual([{ title: 'T', verdict: 'accept' }])
  })

  it('every co-sign arc in the real huddle ends on a compact row both sides', () => {
    const rows = new Set(h.cells.flatMap((c) => c.block ? [
      ...proposalLines(c.block as Block, c.member).map((l) => l.anchor),
      ...answerLines(c.block as Block, c.member).map((l) => l.anchor),
    ] : []))
    const answered = arcsFor(h).filter((a) => a.state !== 'pending' && !a.to.startsWith('head-'))
    expect(answered.length).toBe(6)
    for (const a of answered) {
      expect(rows.has(anchorKey.answer(a.partner, a.title))).toBe(true)
      expect(rows.has(anchorKey.proposal(a.lead, a.title))).toBe(true)
    }
  })
})

describe('flow geometry', () => {
  const card = (x: number, y: number) => ({ x, y, w: 200, h: 80 })
  const row = (c: { x: number; y: number }, dy: number) => ({ x: c.x + 10, y: c.y + dy, w: 180, h: 20 })

  it('anchors an arc to the card edge, level with the answer row and the proposal row', () => {
    const answerCard = card(600, 300) // right of the proposal: leaves by its left edge
    const proposalCard = card(100, 150)
    const { start, end, d } = flowArc(row(answerCard, 40), answerCard, row(proposalCard, 20), proposalCard)
    expect(start).toEqual({ x: 600, y: 300 + 40 + 10 })
    expect(end).toEqual({ x: 300, y: 150 + 20 + 10 })
    expect(d.startsWith('M 600 350 C')).toBe(true)
    expect(d.endsWith('300 180')).toBe(true)
    // Mirrored: an answer left of its proposal leaves by the right edge.
    expect(flowArc(row(proposalCard, 0), proposalCard, row(answerCard, 0), answerCard).start.x).toBe(300)
    expect(rowAnchor(row(answerCard, 0), answerCard, 'right').x).toBe(800)
  })

  it('same column: out of and back into the left edges', () => {
    const a = card(100, 300), b = card(100, 100)
    const { start, end } = flowArc(row(a, 0), a, row(b, 0), b)
    expect([start.x, end.x]).toEqual([100, 100])
  })

  it('ask and reply run in the gutters above and below the row', () => {
    const leader = { x: 0, y: 100, w: 200, h: 60 }
    const member = { x: 500, y: 100, w: 200, h: 90 }
    expect(askPath(leader, member, 88)).toBe('M 200 114 L 206 114 L 206 88 L 522 88 L 522 100')
    expect(replyPath(member, leader, 202)).toBe('M 678 190 L 678 202 L 178 202 L 178 160')
  })
})
