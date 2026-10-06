import { useState, type ReactNode } from 'react'
import { anchorKey, normAnswer, normResolution, type Arc, type ArcState, type Block } from './huddleModel'

/**
 * A member's ```huddle reply, rendered as what it SAYS — never the raw JSON.
 *
 * Known keys render in reading order with plain headings; anything a newer
 * huddle type adds that this page does not know yet falls through to a
 * collapsed JSON view, so a new type is readable before it is pretty.
 */

const ENVELOPE = new Set(['huddle', 'round', 'member'])
const KNOWN = [
  'worked_on', 'priorities', 'projects', 'offers', 'needs',
  'proposals', 'critique_answers', 'answers', 'resolutions', 'feedback',
] as const

export const ANSWER_STYLE: Record<ArcState, { pill: string; label: string }> = {
  'co-sign': { pill: 'bg-success/15 text-success border-success/40', label: 'co-sign' },
  amend: { pill: 'bg-warning/15 text-warning border-warning/40', label: 'amend' },
  decline: { pill: 'bg-destructive/15 text-destructive border-destructive/40', label: 'decline' },
  pending: { pill: 'bg-muted text-muted-foreground border-border border-dashed', label: 'pending' },
  'amend-accepted': { pill: 'bg-success/15 text-success border-success/40', label: 'amend accepted' },
  'amend-rejected': { pill: 'bg-destructive/15 text-destructive border-destructive/40', label: 'amend rejected' },
}

const RESOLUTION_PILL = {
  accept: 'bg-success/15 text-success border-success/40',
  reject: 'bg-destructive/15 text-destructive border-destructive/40',
} as const

function asList(v: unknown): unknown[] {
  return Array.isArray(v) ? v : []
}

function text(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  return JSON.stringify(v)
}

function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <section className="space-y-1.5">
      <h4 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">{label}</h4>
      {children}
    </section>
  )
}

/** A bulleted list that folds after three, so one long reply cannot push its
 * whole row off the screen. */
function Lines({ items }: { items: unknown[] }) {
  const [open, setOpen] = useState(false)
  const shown = open ? items : items.slice(0, 3)
  return (
    <ul className="space-y-1">
      {shown.map((it, i) => (
        <li key={i} className="flex gap-2 text-[13px] leading-snug text-foreground">
          <span aria-hidden className="mt-[7px] size-1 shrink-0 rounded-full bg-muted-foreground/60" />
          <span className="min-w-0 break-words">{text(it)}</span>
        </li>
      ))}
      {items.length > 3 && (
        <li>
          <button
            type="button"
            onClick={() => setOpen(!open)}
            className="text-[12px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
          >
            {open ? 'show less' : `show ${items.length - 3} more`}
          </button>
        </li>
      )}
    </ul>
  )
}

export function AnswerPill({ answer, title }: { answer: ArcState; title?: string }) {
  const s = ANSWER_STYLE[answer]
  return (
    <span
      data-answer={answer}
      title={title}
      className={`inline-flex h-5 items-center rounded-full border px-2 text-[11px] font-medium ${s.pill}`}
    >
      {s.label}
    </span>
  )
}

function Chip({ children, tone = 'muted' }: { children: ReactNode; tone?: 'muted' | 'lead' }) {
  return (
    <span
      className={
        'inline-flex h-5 items-center gap-1 rounded-full border px-2 text-[11px] font-medium ' +
        (tone === 'lead' ? 'border-primary/40 bg-primary/10 text-primary' : 'border-border bg-muted/60 text-foreground-secondary')
      }
    >
      {children}
    </span>
  )
}

type ProposalRow = {
  title?: unknown; lead?: unknown; with?: unknown; priority?: unknown; project?: unknown
  why?: unknown; plan?: unknown; effort?: unknown; confidence?: unknown
  success_measure?: unknown; ask_of_partners?: unknown
}

/** `anchored` is false for a revised copy (a round-4 accept), so the arcs keep
 * ending on the round-2 card where the proposal was made. */
function Proposal({ p, member, arcs, anchored = true }: { p: ProposalRow; member: string; arcs: Arc[]; anchored?: boolean }) {
  const lead = text(p.lead) || member
  const title = text(p.title)
  const partners = asList(p.with).map(text).filter((m) => m && m !== lead)
  const project = p.project && typeof p.project === 'object' ? (p.project as { name?: unknown; new?: unknown }) : null
  const confidence = typeof p.confidence === 'number' ? Math.round(p.confidence * 100) : null
  const asks = p.ask_of_partners && typeof p.ask_of_partners === 'object' ? (p.ask_of_partners as Record<string, unknown>) : {}
  const stateOf = (partner: string): ArcState =>
    arcs.find((a) => a.partner === partner && a.lead === lead && a.title.toLowerCase() === title.toLowerCase())?.state ?? 'pending'

  return (
    <article
      data-proposal={anchored ? '' : 'revised'}
      data-anchor={anchored ? anchorKey.proposal(lead, title) : undefined}
      className="rounded-lg border border-border bg-background/60 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h5 className="text-[13px] font-semibold leading-snug text-foreground">{title || 'Untitled proposal'}</h5>
        <div className="flex shrink-0 items-center gap-1.5 text-[11px] text-muted-foreground">
          {text(p.effort) && <span className="rounded border border-border px-1.5 font-mono">{text(p.effort)}</span>}
          {confidence !== null && <span>{confidence}% sure</span>}
        </div>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <Chip tone="lead">{lead} leads</Chip>
        {partners.map((m) => {
          const st = stateOf(m)
          return (
            <span key={m} data-partner-state={st} className="inline-flex items-center gap-1">
              <Chip>with {m}</Chip>
              <AnswerPill answer={st} title={`${m}: ${ANSWER_STYLE[st].label}`} />
            </span>
          )
        })}
      </div>
      {(text(p.priority) || project) && (
        <p className="mt-2 text-[12px] leading-snug text-muted-foreground">
          {text(p.priority) && <>Serves <span className="text-foreground-secondary">“{text(p.priority)}”</span></>}
          {project?.name ? (
            <>
              {text(p.priority) ? ' · ' : ''}project <span className="text-foreground-secondary">{text(project.name)}</span>
              {project.new ? ' (new)' : ''}
            </>
          ) : null}
        </p>
      )}
      {text(p.why) && <p className="mt-2 text-[13px] leading-snug text-foreground">{text(p.why)}</p>}
      {asList(p.plan).length > 0 && (
        <ol className="mt-2 list-decimal space-y-0.5 pl-5 text-[12px] text-foreground-secondary">
          {asList(p.plan).map((s, i) => <li key={i}>{text(s)}</li>)}
        </ol>
      )}
      {Object.keys(asks).length > 0 && (
        <dl className="mt-2 space-y-0.5 text-[12px]">
          {Object.entries(asks).map(([m, ask]) => (
            <div key={m} className="flex gap-1.5">
              <dt className="shrink-0 font-medium text-foreground-secondary">Asks {m}:</dt>
              <dd className="text-muted-foreground">{text(ask)}</dd>
            </div>
          ))}
        </dl>
      )}
      {text(p.success_measure) && (
        <p className="mt-2 text-[12px] text-muted-foreground">
          <span className="font-medium text-foreground-secondary">Done when</span> {text(p.success_measure)}
        </p>
      )}
    </article>
  )
}

export function BlockView({ block, member, arcs = [] }: { block: Block; member: string; arcs?: Arc[] }) {
  const rest = Object.entries(block).filter(([k]) => !ENVELOPE.has(k) && !(KNOWN as readonly string[]).includes(k))
  const has = (k: string) => {
    const v = block[k]
    return Array.isArray(v) ? v.length > 0 : v !== undefined && v !== null && v !== ''
  }

  if (!KNOWN.some(has) && rest.length === 0) {
    return <p className="text-[12px] italic text-muted-foreground">Replied — nothing to add this round.</p>
  }

  return (
    <div className="space-y-3">
      {has('worked_on') && <Section label="Worked on"><Lines items={asList(block.worked_on)} /></Section>}
      {has('priorities') && <Section label="Priorities as I see them"><Lines items={asList(block.priorities)} /></Section>}
      {has('projects') && (
        <Section label="Projects">
          <ul className="space-y-1">
            {asList(block.projects).map((raw, i) => {
              const p = (raw && typeof raw === 'object' ? raw : { name: raw }) as Record<string, unknown>
              return (
                <li key={i} className="text-[13px] leading-snug">
                  <span className="font-medium text-foreground">{text(p.name)}</span>
                  {text(p.state) && <span className="text-muted-foreground"> — {text(p.state)}</span>}
                  {text(p.next) && <span className="text-foreground-secondary"> → {text(p.next)}</span>}
                </li>
              )
            })}
          </ul>
        </Section>
      )}
      {has('offers') && <Section label="Can offer"><Lines items={asList(block.offers)} /></Section>}
      {has('needs') && <Section label="Needs"><Lines items={asList(block.needs)} /></Section>}
      {has('proposals') && (
        <Section label="Proposes">
          <div className="space-y-2">
            {asList(block.proposals).map((p, i) => (
              <Proposal key={i} p={(p ?? {}) as ProposalRow} member={member} arcs={arcs} />
            ))}
          </div>
        </Section>
      )}
      {has('critique_answers') && (
        <Section label="Answers the critique">
          <ul className="space-y-1.5">
            {asList(block.critique_answers).map((raw, i) => {
              const a = (raw ?? {}) as Record<string, unknown>
              return (
                <li key={i} className="text-[13px] leading-snug">
                  {text(a.title) && <span className="font-medium text-foreground">{text(a.title)}: </span>}
                  <span className="text-foreground-secondary">{text(a.answer)}</span>
                </li>
              )
            })}
          </ul>
        </Section>
      )}
      {has('answers') && (
        <Section label="Answers">
          <ul className="space-y-2">
            {asList(block.answers).map((raw, i) => {
              const a = (raw ?? {}) as Record<string, unknown>
              return (
                <li key={i} data-anchor={anchorKey.answer(member, text(a.title))} className="space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <AnswerPill answer={normAnswer(a.answer)} />
                    <span className="text-[13px] font-medium text-foreground">{text(a.title)}</span>
                    {text(a.lead) && <span className="text-[11px] text-muted-foreground">lead {text(a.lead)}</span>}
                  </div>
                  {text(a.note) && <p className="pl-1 text-[12px] italic text-muted-foreground">“{text(a.note)}”</p>}
                </li>
              )
            })}
          </ul>
        </Section>
      )}
      {has('resolutions') && (
        <Section label="Resolves the amends">
          <ul className="space-y-2">
            {asList(block.resolutions).map((raw, i) => {
              const r = (raw ?? {}) as Record<string, unknown>
              const verdict = normResolution(r.resolution)
              const revised = verdict === 'accept' && r.proposal && typeof r.proposal === 'object'
                ? (r.proposal as ProposalRow) : null
              return (
                <li key={i} data-resolution={verdict ?? 'unknown'} className="space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    {verdict && (
                      <span className={`inline-flex h-5 items-center rounded-full border px-2 text-[11px] font-medium ${RESOLUTION_PILL[verdict]}`}>
                        {verdict}
                      </span>
                    )}
                    <span className="text-[13px] font-medium text-foreground">{text(r.title)}</span>
                  </div>
                  {text(r.note) && <p className="pl-1 text-[12px] italic text-muted-foreground">“{text(r.note)}”</p>}
                  {revised && (
                    <div className="pt-1">
                      <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Revised proposal</div>
                      <Proposal p={revised} member={member} arcs={arcs} anchored={false} />
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        </Section>
      )}
      {has('feedback') && (
        <Section label="Feedback on the huddle">
          <p className="text-[12px] italic leading-snug text-muted-foreground">{text(block.feedback)}</p>
        </Section>
      )}
      {rest.map(([k, v]) => (
        <details key={k} className="text-[12px]">
          <summary className="cursor-pointer text-muted-foreground">{k}</summary>
          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded bg-muted/50 p-2 text-[11px] text-foreground-secondary">
            {JSON.stringify(v, null, 2)}
          </pre>
        </details>
      ))}
    </div>
  )
}
