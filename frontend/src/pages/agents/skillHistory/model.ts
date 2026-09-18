/**
 * Everything the History page derives from the payload, with no React in it.
 *
 * Ported from the approved prototype (docs/superpowers/specs/
 * 2026-09-18-agent-skill-history-design.md). The page shows counts, dates and
 * commit messages only — nothing here produces a characterisation.
 */
import type { SkillHistoryOut } from '@/api/agents'

export type Selection = { group?: string; skill?: string; commit?: string }
export type Rev = { commit: number; day: number; lines: number; added: number; deleted: number }
export type SkillInfo = {
  name: string
  revs: Rev[]
  firstDay: number
  removedDay: number | null
  checks: string | null
  checkedBy: string[]
  group: number | null
}
export type Model = {
  h: SkillHistoryOut
  start: number // epoch ms of day 0 (UTC)
  days: number // index of the last day
  commitDay: number[]
  commitSkills: string[][]
  skills: Map<string, SkillInfo>
  bySha: Map<string, number>
}

const DAY = 86_400_000
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const utc = (iso: string) => Date.parse(iso.slice(0, 10) + 'T00:00:00Z')

export function buildModel(h: SkillHistoryOut): Model {
  const start = h.commits.length ? utc(h.commits[0].date) : Date.now()
  const endIso = h.synced_at ?? h.commits.at(-1)?.date ?? new Date().toISOString()
  const days = Math.max(0, Math.round((utc(endIso) - start) / DAY))
  const commitDay = h.commits.map((c) => Math.round((utc(c.date) - start) / DAY))
  const commitSkills: string[][] = h.commits.map(() => [])
  const present = new Set(h.present)
  const groupOf = new Map<string, number>()
  h.groups.forEach((g, i) => g.skills.forEach((s) => groupOf.set(s, i)))
  const skills = new Map<string, SkillInfo>()
  for (const s of h.skills) {
    const revs = s.revisions.map(([commit, lines, added, deleted]) => ({ commit, day: commitDay[commit], lines, added, deleted }))
    revs.forEach((r) => commitSkills[r.commit].push(s.name))
    const last = revs.at(-1)
    const checks = h.checks[s.name] ?? null
    skills.set(s.name, {
      name: s.name,
      revs,
      firstDay: revs[0]?.day ?? Infinity,
      removedDay: !present.has(s.name) && last ? last.day : null,
      checks,
      checkedBy: Object.entries(h.checks).filter(([, v]) => v === s.name).map(([k]) => k).sort(),
      group: groupOf.get(s.name) ?? (checks ? groupOf.get(checks) ?? null : null),
    })
  }
  commitSkills.forEach((l) => l.sort())
  return { h, start, days, commitDay, commitSkills, skills, bySha: new Map(h.commits.map((c, i) => [c.sha, i])) }
}

export const dayOf = (m: Model, iso: string) => Math.round((utc(iso) - m.start) / DAY)
export const isoOf = (m: Model, day: number) => new Date(m.start + day * DAY).toISOString().slice(0, 10)
export const fmtDay = (m: Model, day: number) => {
  const d = new Date(m.start + day * DAY)
  return `${MON[d.getUTCMonth()]} ${d.getUTCDate()}`
}

const upto = (s: SkillInfo, day: number) => s.revs.filter((r) => r.day <= day)
const removedBy = (s: SkillInfo, day: number) => s.removedDay !== null && s.removedDay <= day
const linesAt = (s: SkillInfo, day: number) => upto(s, day).at(-1)?.lines ?? 0

export function totalsAt(m: Model, day: number) {
  let skills = 0, revisions = 0, withChecks = 0, removed = 0
  for (const s of m.skills.values()) {
    const n = upto(s, day).length
    revisions += n
    if (n > 0 && !removedBy(s, day)) skills++
    if (removedBy(s, day)) removed++
    if (!s.checks && n > 0 && !removedBy(s, day) && s.checkedBy.some((c) => upto(m.skills.get(c)!, day).length > 0)) withChecks++
  }
  return { skills, revisions, withChecks, removed }
}

export type Tile = { name: string; state: 'unborn' | 'active' | 'recent' | 'removed'; revisions: number; checkRevisions: number; lines: number; selected: boolean }
export type GroupRow = { index: number; title: string; kind: string; num: string; meta: string; dimmed: boolean; tiles: Tile[] }

export function selectedGroup(m: Model, sel: Selection): number | null {
  if (sel.group !== undefined) return m.h.groups.findIndex((g) => g.title === sel.group)
  if (sel.skill) return m.skills.get(sel.skill)?.group ?? null
  return null
}

export function tilesAt(m: Model, day: number, sel: Selection): GroupRow[] {
  const focus = selectedGroup(m, sel)
  return m.h.groups.map((g, index) => {
    let active = 0, revs = 0
    const tiles = g.skills.map((name): Tile => {
      const s = m.skills.get(name)!
      const n = upto(s, day).length
      const checkRevisions = s.checkedBy.reduce((a, c) => a + upto(m.skills.get(c)!, day).length, 0)
      revs += n + checkRevisions
      const recent = [s, ...s.checkedBy.map((c) => m.skills.get(c)!)].some((x) => x.revs.some((r) => r.day <= day && r.day > day - 7))
      const state = n === 0 ? 'unborn' : removedBy(s, day) ? 'removed' : recent ? 'recent' : 'active'
      if (state === 'active' || state === 'recent') active++
      return { name, state, revisions: n, checkRevisions, lines: linesAt(s, day),
               selected: sel.skill === name || s.checkedBy.includes(sel.skill ?? '') }
    })
    return { index, title: g.title, kind: g.kind, num: g.num, meta: `${active} skills · ${revs} revisions`,
             dimmed: focus !== null && focus !== index, tiles }
  })
}

export function skillsSeries(m: Model): number[] {
  return Array.from({ length: m.days + 1 }, (_, d) => totalsAt(m, d).skills)
}

export function weeklyCounts(m: Model, names: string[] | null): number[] {
  const weeks = new Array(Math.floor(m.days / 7) + 1).fill(0)
  const pool = names ?? [...m.skills.keys()]
  for (const n of pool) m.skills.get(n)?.revs.forEach((r) => { weeks[Math.floor(r.day / 7)]++ })
  return weeks
}

/** Skill names the chart highlights for a selection (null = none). */
export function scopeNames(m: Model, sel: Selection): string[] | null {
  if (sel.commit !== undefined) return m.commitSkills[m.bySha.get(sel.commit) ?? -1] ?? []
  if (sel.skill) return [sel.skill]
  const gi = selectedGroup(m, sel)
  if (gi === null || gi < 0) return null
  return m.h.groups[gi].skills.flatMap((s) => [s, ...(m.skills.get(s)?.checkedBy ?? [])])
}

const change = (r: Rev, prev: Rev | undefined) =>
  !prev ? 'created' : r.lines === 0 && r.deleted > 0 ? 'removed' : `${r.lines - prev.lines >= 0 ? '+' : '−'}${Math.abs(r.lines - prev.lines)}`

type CommitRow = { sha: string; date: string; subject: string; skills: string[] }
export type Panel =
  | { kind: 'all'; week: CommitRow[]; top: { name: string; revisions: number }[] }
  | { kind: 'group'; title: string; kindLabel: string; skills: number; revisions: number; first: string | null;
      rows: { name: string; first: string | null; revisions: number; lines: number | null; checkedBy: string[] }[]; recent: CommitRow[] }
  | { kind: 'skill'; name: string; group: string | null; created: string; lastRevised: string | null; removed: string | null;
      revisions: number; lines: number | null; firstLines: number; checks: string | null; checkedBy: string[];
      series: { day: number; lines: number }[];
      list: { sha: string; date: string; subject: string; change: string; later: boolean }[] }
  | { kind: 'commit'; sha: string; date: string; subject: string; rows: { name: string; change: string; lines: number }[] }

const commitRow = (m: Model, i: number): CommitRow => ({
  sha: m.h.commits[i].sha, date: fmtDay(m, m.commitDay[i]), subject: m.h.commits[i].subject, skills: m.commitSkills[i],
})

export function panelAt(m: Model, day: number, sel: Selection): Panel {
  if (sel.commit !== undefined) {
    const i = m.bySha.get(sel.commit)
    if (i !== undefined) {
      return { kind: 'commit', sha: m.h.commits[i].sha, date: fmtDay(m, m.commitDay[i]), subject: m.h.commits[i].subject,
        rows: m.commitSkills[i].map((name) => {
          const s = m.skills.get(name)!
          const k = s.revs.findIndex((r) => r.commit === i)
          return { name, change: change(s.revs[k], s.revs[k - 1]), lines: s.revs[k].lines }
        }) }
    }
  }
  if (sel.skill && m.skills.has(sel.skill)) {
    const s = m.skills.get(sel.skill)!
    const past = upto(s, day)
    return { kind: 'skill', name: s.name, group: s.group !== null ? m.h.groups[s.group].title : null,
      created: fmtDay(m, s.firstDay), lastRevised: past.length ? fmtDay(m, past.at(-1)!.day) : null,
      removed: s.removedDay !== null ? fmtDay(m, s.removedDay) : null,
      revisions: past.length, lines: removedBy(s, day) || !past.length ? null : past.at(-1)!.lines,
      firstLines: s.revs[0]?.lines ?? 0, checks: s.checks, checkedBy: s.checkedBy,
      series: s.revs.map((r) => ({ day: r.day, lines: r.lines })),
      list: s.revs.map((r, k) => ({ sha: m.h.commits[r.commit].sha, date: fmtDay(m, r.day),
        subject: m.h.commits[r.commit].subject, change: change(r, s.revs[k - 1]), later: r.day > day })).reverse() }
  }
  const gi = selectedGroup(m, sel)
  if (gi !== null && gi >= 0) {
    const g = m.h.groups[gi]
    const names = scopeNames(m, sel) ?? []
    const commits = new Set<number>()
    names.forEach((n) => upto(m.skills.get(n)!, day).forEach((r) => commits.add(r.commit)))
    const firsts = names.map((n) => m.skills.get(n)!.firstDay).filter((d) => d <= day)
    return { kind: 'group', title: g.title, kindLabel: g.kind === 'phase' ? `Phase ${g.num}` : g.kind === 'agent' ? 'Agent' : 'No agent',
      skills: g.skills.filter((n) => upto(m.skills.get(n)!, day).length && !removedBy(m.skills.get(n)!, day)).length,
      revisions: names.reduce((a, n) => a + upto(m.skills.get(n)!, day).length, 0),
      first: firsts.length ? fmtDay(m, Math.min(...firsts)) : null,
      rows: g.skills.map((n) => {
        const s = m.skills.get(n)!
        const k = upto(s, day).length
        return { name: n, first: s.firstDay <= day ? fmtDay(m, s.firstDay) : null, revisions: k,
                 lines: k && !removedBy(s, day) ? linesAt(s, day) : null, checkedBy: s.checkedBy }
      }).sort((a, b) => b.revisions - a.revisions),
      recent: [...commits].sort((a, b) => b - a).slice(0, 10).map((i) => commitRow(m, i)) }
  }
  const week = m.commitDay.map((d, i) => ({ d, i })).filter(({ d }) => d <= day && d > day - 7).reverse().map(({ i }) => commitRow(m, i))
  const top = [...m.skills.values()].map((s) => ({ name: s.name, revisions: upto(s, day).length }))
    .filter((t) => t.revisions > 0).sort((a, b) => b.revisions - a.revisions).slice(0, 10)
  return { kind: 'all', week, top }
}
