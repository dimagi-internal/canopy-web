import type { HuddleCell } from '@/api/huddles'
import { AnswerPill, ANSWER_STYLE } from './BlockView'
import {
  answerLines, critiqueAnswered, proposalLines, reportSummary, resolutionLines,
  type Arc, type ArcState, type Block,
} from './huddleModel'

/**
 * A member's reply, compact: two to four single lines that say what the reply
 * DID this round, so a whole round fits on one screen row and the flow between
 * agents shows. Each proposal line and answer row carries the same
 * `data-anchor` as its full rendering, so the co-sign arcs end on it.
 */

const RESOLUTION_PILL = {
  accept: 'bg-success/15 text-success border-success/40',
  reject: 'bg-destructive/15 text-destructive border-destructive/40',
} as const

const DOT: Record<ArcState, string> = {
  'co-sign': 'bg-success',
  amend: 'bg-warning',
  decline: 'bg-destructive',
  pending: 'border border-dashed border-muted-foreground bg-transparent',
  'amend-accepted': 'bg-success',
  'amend-rejected': 'bg-destructive',
}

function Line({ children, className = '', anchor }: { children: React.ReactNode; className?: string; anchor?: string }) {
  return (
    <div data-anchor={anchor} className={'flex min-w-0 items-center gap-1.5 text-[12px] leading-5 ' + className}>
      {children}
    </div>
  )
}

const more = (n: number) => (n > 0 ? <Line className="text-muted-foreground">+{n} more</Line> : null)

export function CompactReply({ block, member, leader, round, arcs }: {
  block: Block; member: string; leader: string; round: number; arcs: Arc[]
}) {
  const props = proposalLines(block, member)
  const answers = answerLines(block, member)
  const resolutions = resolutionLines(block)

  if (round === 1 || (!props.length && !answers.length && !resolutions.length && Array.isArray(block.priorities))) {
    const { stats, top } = reportSummary(block)
    return (
      <div data-compact="report" className="space-y-0.5">
        <Line className="font-medium text-foreground-secondary">{stats || 'reported'}</Line>
        {top && (
          <p className="line-clamp-2 text-[12px] leading-snug text-foreground" title={top}>
            <span className="text-muted-foreground">Top priority: </span>{top}
          </p>
        )}
      </div>
    )
  }

  if (answers.length || round === 3) {
    const shown = answers.slice(0, 4)
    return (
      <div data-compact="answers" className="space-y-0.5">
        {shown.length === 0 && <Line className="italic text-muted-foreground">no joint work to answer</Line>}
        {shown.map((a, i) => (
          <Line key={i} anchor={a.anchor}>
            <AnswerPill answer={a.answer} />
            <span className="min-w-0 truncate text-foreground" title={a.title}>{a.title}</span>
          </Line>
        ))}
        {more(answers.length - shown.length)}
        {props.length > 0 && <Line className="text-muted-foreground">revised {props.length} proposal{props.length === 1 ? '' : 's'}</Line>}
      </div>
    )
  }

  if (resolutions.length || round === 4) {
    return (
      <div data-compact="resolutions" className="space-y-0.5">
        {resolutions.length === 0 && <Line className="italic text-muted-foreground">nothing to resolve</Line>}
        {resolutions.slice(0, 4).map((r, i) => (
          <Line key={i}>
            {r.verdict && (
              <span className={`inline-flex h-5 shrink-0 items-center rounded-full border px-2 text-[11px] font-medium ${RESOLUTION_PILL[r.verdict]}`}>
                {r.verdict}
              </span>
            )}
            <span className="min-w-0 truncate text-foreground" title={r.title}>{r.title}</span>
          </Line>
        ))}
        {more(resolutions.length - 4)}
      </div>
    )
  }

  // Round 2 (and any proposing round).
  const answered = critiqueAnswered(block, leader)
  const stateOf = (lead: string, title: string, partner: string): ArcState =>
    arcs.find((a) => a.partner === partner && a.lead === lead && a.title.toLowerCase() === title.toLowerCase())?.state ?? 'pending'
  const shown = props.slice(0, 3)
  return (
    <div data-compact="proposals" className="space-y-0.5">
      {shown.length === 0 && <Line className="italic text-muted-foreground">no proposals</Line>}
      {shown.map((p, i) => (
        <Line key={i} anchor={p.anchor}>
          <span aria-hidden className="text-muted-foreground">▸</span>
          <span className="min-w-0 flex-1 truncate font-medium text-foreground" title={p.title}>{p.title}</span>
          {p.partners.length > 0 ? (
            <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border bg-muted/60 px-1.5 text-[10px] text-foreground-secondary">
              with {p.partners.join(', ')}
              {p.partners.map((m) => {
                const st = stateOf(p.lead, p.title, m)
                return <span key={m} title={`${m}: ${ANSWER_STYLE[st].label}`} data-partner-dot={st} className={`size-1.5 rounded-full ${DOT[st]}`} />
              })}
            </span>
          ) : (
            <span className="shrink-0 text-[10px] text-muted-foreground">solo</span>
          )}
        </Line>
      ))}
      {more(props.length - shown.length)}
      {answered && <Line className="text-muted-foreground">↩ {answered}</Line>}
    </div>
  )
}

/** A non-reply cell's status, in one line. */
export function CompactStatus({ cell, state }: { cell: HuddleCell; state: string }) {
  if (state === 'waiting') {
    return (
      <Line className="text-info">
        <span className="relative flex size-2">
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-info/60" />
          <span className="relative inline-flex size-2 rounded-full bg-info" />
        </span>
        Waiting for {cell.member}…
      </Line>
    )
  }
  if (state === 'hidden') return <Line className="text-muted-foreground">Ran — content hidden from you</Line>
  return (
    <Line className={state === 'failed' ? 'text-destructive' : 'text-muted-foreground'}>
      {state === 'failed' ? 'Ended without a reply' : 'No reply block'}
    </Line>
  )
}
