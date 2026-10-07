/**
 * The huddle's outcome in one or two sentences — the headline and "Your move"
 * — for the summary above every view. Plain words only (plainWords).
 */
import type { Huddle, HuddleOutput } from '@/api/huddles'
import { columns, type ProposalOutcome } from './huddleModel'
import { nextStep } from './plainWords'

export type Output = HuddleOutput & { next_action?: string | null }

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

/** Where things stand on the agents' boards, counted. */
export function boardTally(huddle: Huddle) {
  const outputs = huddle.outputs as Output[]
  const steps = outputs.map((o) => ({ o, s: nextStep(o, columns(huddle)) }))
  return {
    waiting: outputs.filter((o) => o.status === 'suggested').length,
    stuck: steps.filter((x) => x.s.stuck).length,
    onYou: steps.filter((x) => x.s.onYou).length,
  }
}

/** The headline sentence: what was decided, and how ideas map to tasks. */
export function headline(filed: ProposalOutcome[], tasks: number, held: number, open: number, finished: boolean): string {
  if (!finished) {
    const parts = [filed.length && `${plural(filed.length, 'idea')} agreed`, open && `${open} still being worked out`].filter(Boolean)
    return parts.length ? `Still going: ${parts.join(', ')}.` : 'Still going — no ideas yet.'
  }
  if (filed.length === 0) return held ? `Nothing was sent to you — ${plural(held, 'idea')} parked.` : 'Nobody suggested anything.'
  const joint = filed.filter((p) => p.tasks.length > 1).length
  let s = `${plural(filed.length, 'idea')} sent to you to decide`
  if (tasks) s += `, as ${plural(tasks, 'task')} on the agents' boards`
  if (joint && tasks > filed.length) s += ` (a shared idea puts one task on each agent's board)`
  s += '.'
  if (held) s += ` ${plural(held, 'idea')} parked.`
  return s
}

/** Your move, in one line: what waits on the reader, and anything stuck. */
export function yourMove(huddle: Huddle): string {
  const { waiting, stuck, onYou } = boardTally(huddle)
  const parts: string[] = []
  if (waiting) parts.push(`${waiting === 1 ? 'one task is' : `${waiting} tasks are`} waiting for your yes/no`)
  if (onYou) parts.push(`${onYou === 1 ? 'one thing is' : `${onYou} things are`} stuck waiting on you`)
  if (parts.length) {
    const s = parts.join(', and ')
    return `${s[0].toUpperCase()}${s.slice(1)}${stuck > onYou ? ` (${stuck - onYou} more stuck on something else)` : ''} — each is linked below.`
  }
  if (stuck) return `Nothing needs you, but ${stuck === 1 ? 'one task is' : `${stuck} tasks are`} stuck — see why below.`
  return huddle.outputs.length ? 'Nothing is waiting on you — you have answered every task.' : ''
}


export type IdeaState = { tone: 'you' | 'stuck' | 'moving' | 'done' | 'no' | 'parked'; text: string }

/** Where one idea stands, in two or three words, for the at-a-glance list. */
export function ideaState(p: ProposalOutcome, agents: string[]): IdeaState {
  if (p.verdict !== 'filed') return { tone: 'parked', text: p.verdict === 'open' ? 'Still being worked out' : 'Parked' }
  const ts = p.tasks as Output[]
  if (!ts.length) return { tone: 'moving', text: 'Agreed' }
  if (ts.every((t) => t.status === 'declined')) return { tone: 'no', text: 'You said no' }
  const steps = ts.map((t) => nextStep(t, agents))
  if (steps.some((s) => s.onYou)) return { tone: 'you', text: 'Stuck — needs you' }
  if (ts.some((t) => t.status === 'suggested')) return { tone: 'you', text: 'Needs your yes/no' }
  if (steps.some((s) => s.stuck)) return { tone: 'stuck', text: 'Stuck' }
  if (ts.every((t) => t.status === 'done' || t.status === 'declined')) return { tone: 'done', text: 'Done' }
  return { tone: 'moving', text: 'Under way' }
}
