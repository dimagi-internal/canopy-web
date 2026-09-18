import { describe, expect, it } from 'vitest'
import type { SkillHistoryOut } from '@/api/agents'
import { buildModel, dayOf, fmtDay, panelAt, tilesAt, totalsAt, weeklyCounts } from './model'

const H = {
  agent: 'ace', repo_url: 'x', head_sha: 'b', synced_at: '2026-04-20T00:00:00Z', synced_with: 'o',
  last_error: '', credential_state: 'ok',
  groups: [{ title: 'One', kind: 'phase', num: '01', skills: ['alpha'] },
           { title: 'Not assigned to an agent', kind: 'none', num: '', skills: ['gone'] }],
  checks: { 'alpha-eval': 'alpha' },
  present: ['alpha', 'alpha-eval'],
  commits: [
    { sha: 'a1', date: '2026-04-01', subject: 'feat: alpha' },
    { sha: 'b2', date: '2026-04-05', subject: 'fix: alpha and its eval' },
    { sha: 'c3', date: '2026-04-10', subject: 'chore: retire gone' },
  ],
  skills: [
    { name: 'alpha', revisions: [[0, 10, 10, 0], [1, 12, 2, 0]] },
    { name: 'alpha-eval', revisions: [[1, 4, 4, 0]] },
    { name: 'gone', revisions: [[0, 3, 3, 0], [2, 0, 0, 3]] },
  ],
} as unknown as SkillHistoryOut

const m = buildModel(H)

describe('skill history model', () => {
  it('lays days out from the first commit to the sync', () => {
    expect(dayOf(m, '2026-04-01')).toBe(0)
    expect(m.days).toBe(19)
    expect(fmtDay(m, 4)).toBe('Apr 5')
  })

  it('counts what existed on a given day', () => {
    expect(totalsAt(m, 0)).toEqual({ skills: 2, revisions: 2, withChecks: 0, removed: 0 })
    expect(totalsAt(m, 19)).toEqual({ skills: 2, revisions: 5, withChecks: 1, removed: 1 })
  })

  it('gives tiles their state on a day, with checking skills inside the skill they check', () => {
    const rows = tilesAt(m, 4, {})
    const alpha = rows[0].tiles[0]
    expect(alpha).toMatchObject({ name: 'alpha', state: 'recent', revisions: 2, checkRevisions: 1, lines: 12 })
    expect(rows.flatMap((r) => r.tiles.map((t) => t.name))).not.toContain('alpha-eval')
    expect(tilesAt(m, 19, {})[1].tiles[0].state).toBe('removed')
  })

  it('dims every other group when one is selected', () => {
    const rows = tilesAt(m, 19, { group: 'One' })
    expect(rows.map((r) => r.dimmed)).toEqual([false, true])
  })

  it('counts revisions per week for a scope', () => {
    expect(weeklyCounts(m, null)).toEqual([4, 1, 0])
    expect(weeklyCounts(m, ['alpha'])).toEqual([2, 0, 0])
  })

  it('builds the all-skills panel from the 7 days to the date', () => {
    const p = panelAt(m, 8, {})
    expect(p.kind).toBe('all')
    if (p.kind !== 'all') return
    expect(p.week.map((c) => c.subject)).toEqual(['fix: alpha and its eval'])
    expect(p.week[0].skills).toEqual(['alpha', 'alpha-eval'])
    expect(p.top[0]).toMatchObject({ name: 'alpha', revisions: 2 })
  })

  it('builds the skill panel, fading later revisions', () => {
    const p = panelAt(m, 2, { skill: 'alpha' })
    expect(p.kind).toBe('skill')
    if (p.kind !== 'skill') return
    expect(p.revisions).toBe(1)
    expect(p.checkedBy).toEqual(['alpha-eval'])
    expect(p.list.map((r) => [r.subject, r.later, r.change])).toEqual([
      ['fix: alpha and its eval', true, '+2'],
      ['feat: alpha', false, 'created'],
    ])
  })

  it('builds the commit panel with each skill it changed', () => {
    const p = panelAt(m, 19, { commit: 'c3' })
    expect(p.kind).toBe('commit')
    if (p.kind !== 'commit') return
    expect(p.rows).toEqual([{ name: 'gone', change: 'removed', lines: 0 }])
  })
})
