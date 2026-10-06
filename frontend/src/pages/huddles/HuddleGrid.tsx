import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { Huddle, HuddleCell } from '@/api/huddles'
import { TurnTranscript } from '@/components/activity/TurnTranscript'
import { statusToken } from '@/components/activity/turnLog'
import { BlockView } from './BlockView'
import { CompactReply, CompactStatus } from './CompactCard'
import { askPath, flowArc, replyPath, type Rect } from './flowGeometry'
import {
  anchorKey, arcsFor, cellAt, cellState, columns, critiqueFrom, initial, leaderAsks, memberHue, roundAsk, roundName,
  roundsToShow, type Arc, type ArcState, type Block, type LeaderAsk,
} from './huddleModel'

/**
 * The conversation, read like a sequence diagram: time runs top to bottom, one
 * row per round. The LEADER's lane comes first — each round's ask and the
 * questions it put to each member — then one column per member. Every card
 * starts COMPACT (two to four lines), so a whole round fits on one screen row;
 * a card expands in place to the full reply.
 *
 * Lines: a light ask arrow from the leader's round card to each member, and a
 * reply arrow back; the strong coloured arcs tie a joint proposal's line (round
 * 2) to each partner's answer row (round 3). Everything is read from the
 * derived API — the page holds no huddle state of its own.
 */

const ARC_COLOR: Record<ArcState, string> = {
  'co-sign': 'var(--success)',
  amend: 'var(--warning)',
  decline: 'var(--destructive)',
  pending: 'var(--muted-foreground)',
  // A round-4 resolution: an accepted amend is a co-sign, a rejected one holds.
  'amend-accepted': 'var(--success)',
  'amend-rejected': 'var(--destructive)',
}

const ARC_LABEL: Record<ArcState, string> = {
  'co-sign': 'co-signed',
  amend: 'amended',
  decline: 'declined',
  pending: 'not answered',
  'amend-accepted': 'amend accepted',
  'amend-rejected': 'amend rejected',
}

export function MemberAvatar({ slug, hue, size = 'md' }: { slug: string; hue: string; size?: 'sm' | 'md' }) {
  return (
    <span
      aria-hidden
      className={
        'inline-flex shrink-0 items-center justify-center rounded-full font-semibold ring-1 ring-inset ' +
        (size === 'sm' ? 'size-6 text-[11px]' : 'size-9 text-[14px]')
      }
      style={{
        color: hue,
        background: `color-mix(in oklch, ${hue} 16%, transparent)`,
        // ring colour via a CSS var Tailwind's ring utility reads
        ['--tw-ring-color' as string]: `color-mix(in oklch, ${hue} 45%, transparent)`,
      }}
    >
      {initial(slug)}
    </span>
  )
}

/** Phone width: one round per section, no lines. */
function useIsPhone(): boolean {
  const q = '(max-width: 767px)'
  const get = () => typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(q).matches
  const [phone, setPhone] = useState(get)
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mq = window.matchMedia(q)
    const on = () => setPhone(mq.matches)
    mq.addEventListener?.('change', on)
    return () => mq.removeEventListener?.('change', on)
  }, [])
  return phone
}

const LEADER_HUE = 'var(--foreground-secondary)'
const cardKey = { member: (m: string, r: number) => `${m}-${r}`, leader: (r: number) => `leader-${r}` }

function Footer({ cell }: { cell: HuddleCell }) {
  const [open, setOpen] = useState<'' | 'prompt' | 'transcript'>('')
  if (cell.content_hidden || (!cell.prompt && !cell.has_transcript)) return null
  return (
    <>
      <div className="mt-3 flex gap-3 border-t border-border/60 pt-2 text-[11px]">
        {cell.prompt && (
          <button type="button" onClick={() => setOpen(open === 'prompt' ? '' : 'prompt')}
            className="text-muted-foreground hover:text-foreground" aria-expanded={open === 'prompt'}>
            {open === 'prompt' ? 'Hide prompt' : 'Prompt'}
          </button>
        )}
        {cell.has_transcript && (
          <button type="button" onClick={() => setOpen(open === 'transcript' ? '' : 'transcript')}
            className="text-muted-foreground hover:text-foreground" aria-expanded={open === 'transcript'}>
            {open === 'transcript' ? 'Hide transcript' : 'Transcript'}
          </button>
        )}
      </div>
      {open === 'prompt' && (
        <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-muted/50 p-2 text-[11px] leading-relaxed text-foreground-secondary">
          {cell.prompt}
        </pre>
      )}
      {open === 'transcript' && (
        <div className="mt-2 rounded-lg bg-muted/30 p-2">
          <TurnTranscript turnId={cell.turn_id} />
        </div>
      )}
    </>
  )
}

function AskList({ asks }: { asks: LeaderAsk[] }) {
  return (
    <ul className="space-y-1.5">
      {asks.map((a, i) => (
        <li key={i} className="text-[12px] leading-snug text-foreground-secondary">
          {a.about && <span className="mr-1 text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">on {a.about} ·</span>}
          {a.text}
        </li>
      ))}
    </ul>
  )
}

/** The expanded member card's "ada asked" — collapsed behind a small link. */
function LeaderAskedLink({ cell, leader }: { cell: HuddleCell; leader: string }) {
  const [open, setOpen] = useState(false)
  const asks = leaderAsks(cell.prompt, leader)
  const fallback = asks.length ? '' : critiqueFrom(cell.prompt, leader)
  if (!asks.length && !fallback) return null
  return (
    <div className="mb-2">
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
        className="text-[11px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline">
        {open ? `Hide what ${leader} asked` : `${leader} asked${asks.length ? ` (${asks.length})` : ''} ›`}
      </button>
      {open && (
        <figure className="mt-1 rounded-xl rounded-tr-sm border border-border bg-muted px-3 py-2">
          {asks.length ? <AskList asks={asks} /> : (
            <blockquote className="whitespace-pre-wrap text-[12px] leading-snug text-foreground-secondary">{fallback}</blockquote>
          )}
        </figure>
      )}
    </div>
  )
}

function CardHead({ name, hue, cell, expanded }: { name: string; hue: string; cell?: HuddleCell; expanded: boolean }) {
  return (
    <div className="mb-1 flex min-w-0 items-center gap-1.5">
      <MemberAvatar slug={name} hue={hue} size="sm" />
      <span className="truncate text-[12px] font-semibold text-foreground">{name}</span>
      {cell && cell.status !== 'done' && (
        <span className={`inline-flex h-4 items-center rounded-full border px-1.5 text-[10px] font-medium ${statusToken(cell.status)}`}>{cell.status}</span>
      )}
      {cell && cell.attempt > 1 && <span className="text-[10px] text-muted-foreground">attempt {cell.attempt}</span>}
      {cell?.reply_source === 'transcript' && (
        <span className="text-[10px] text-muted-foreground" title="No close-out was filed; read from the turn's transcript">from transcript</span>
      )}
      <span aria-hidden className={'ml-auto shrink-0 text-[10px] text-muted-foreground transition-transform ' + (expanded ? 'rotate-90' : '')}>▸</span>
    </div>
  )
}

function MemberCard({ cell, hue, leader, arcs, expanded, onToggle }: {
  cell: HuddleCell; hue: string; leader: string; arcs: Arc[]; expanded: boolean; onToggle: () => void
}) {
  const state = cellState(cell)
  const k = cardKey.member(cell.member, cell.round)
  const frame =
    'relative block w-full rounded-xl border bg-card px-3 py-2 text-left shadow-sm ' +
    (state === 'waiting' ? 'border-dashed border-info/50' : 'border-border')
  const style = { borderLeftWidth: 3, borderLeftColor: hue }

  if (!expanded) {
    return (
      <button type="button" data-card={k} data-cell-state={state} aria-expanded={false} onClick={onToggle}
        aria-label={`${cell.member}, round ${cell.round} — expand`}
        className={frame + ' cursor-pointer transition-colors hover:border-primary/50 focus-visible:outline-2 focus-visible:outline-primary'}
        style={style}>
        <CardHead name={cell.member} hue={hue} cell={cell} expanded={false} />
        {state === 'replied'
          ? <CompactReply block={cell.block as Block} member={cell.member} leader={leader} round={cell.round} arcs={arcs} />
          : <CompactStatus cell={cell} state={state} />}
      </button>
    )
  }

  return (
    <div data-card={k} data-cell-state={state} data-expanded className={frame} style={style}>
      <button type="button" aria-expanded onClick={onToggle} aria-label={`${cell.member}, round ${cell.round} — collapse`} className="block w-full text-left">
        <CardHead name={cell.member} hue={hue} cell={cell} expanded />
      </button>
      {cell.round > 1 && state === 'replied' && <LeaderAskedLink cell={cell} leader={leader} />}
      {state === 'replied' && <BlockView block={cell.block as Block} member={cell.member} arcs={arcs} />}
      {state === 'waiting' && (
        <div className="space-y-2" aria-live="polite">
          <CompactStatus cell={cell} state={state} />
          <div className="space-y-1.5" aria-hidden>
            <div className="h-2 w-11/12 animate-pulse rounded bg-muted" />
            <div className="h-2 w-8/12 animate-pulse rounded bg-muted" />
          </div>
        </div>
      )}
      {state === 'hidden' && (
        <p className="text-[12px] text-muted-foreground">
          You can see that it ran, not what it said — a turn&apos;s content is for the people who run the agent.
        </p>
      )}
      {(state === 'no-reply' || state === 'failed') && (
        <div className="space-y-1">
          <p className={'text-[13px] ' + (state === 'failed' ? 'text-destructive' : 'text-muted-foreground')}>
            {state === 'failed' ? 'The turn ended without a reply.' : 'No reply block.'}
          </p>
          {cell.reply_error && <p className="font-mono text-[11px] text-warning">{cell.reply_error}</p>}
          {!cell.has_transcript && <p className="text-[11px] text-muted-foreground">Transcript no longer available.</p>}
        </div>
      )}
      <Footer cell={cell} />
    </div>
  )
}

/** The leader's card for one round: what it asked everyone, and the questions
 * it put to each member (read out of that member's round prompt). */
function LeaderCard({ huddle, round, members, expanded, onToggle }: {
  huddle: Huddle; round: number; members: string[]; expanded: boolean; onToggle: () => void
}) {
  const inRound = huddle.cells.filter((c) => c.round === round)
  const live = inRound.some((c) => cellState(c) === 'waiting')
  const replied = inRound.filter((c) => c.block).length
  const sent = members.filter((m) => inRound.some((c) => c.member === m))
  const asks = sent.map((m) => {
    const c = inRound.find((x) => x.member === m)
    return { m, cell: c, asks: c ? leaderAsks(c.prompt, huddle.leader) : [] }
  })
  const asked = asks.filter((a) => a.asks.length > 0)
  const plain = asks.filter((a) => a.asks.length === 0).map((a) => a.m)
  const k = cardKey.leader(round)

  const head = (
    <>
      <div className="flex items-baseline gap-1.5">
        <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Round {round}</span>
        <span className="text-[13px] font-semibold text-foreground">{roundName(huddle.type, round)}</span>
        {live && <span className="size-1.5 animate-pulse rounded-full bg-info" aria-label="in flight" />}
        <span className="ml-auto text-[10px] text-muted-foreground">{inRound.length === 0 ? 'not started' : `${replied}/${inRound.length} replied`}</span>
        <span aria-hidden className={'shrink-0 text-[10px] text-muted-foreground transition-transform ' + (expanded ? 'rotate-90' : '')}>▸</span>
      </div>
      <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[12px] leading-5">
        <MemberAvatar slug={huddle.leader} hue={LEADER_HUE} size="sm" />
        <span className="min-w-0 truncate text-foreground-secondary">
          <span className="font-medium text-foreground">{huddle.leader}</span> asks: {roundAsk(huddle.type, round)}
        </span>
      </div>
    </>
  )
  const frame = 'relative block w-full rounded-xl border border-border bg-muted px-3 py-2 text-left shadow-sm '
  const style = { borderLeftWidth: 3, borderLeftColor: LEADER_HUE }

  if (!expanded) {
    return (
      <button type="button" data-card={k} aria-expanded={false} onClick={onToggle} aria-label={`${huddle.leader}, round ${round} — expand`}
        className={frame + 'cursor-pointer transition-colors hover:border-primary/50'} style={style}>
        {head}
        {asked.slice(0, 5).map((a) => (
          <div key={a.m} data-ask={`${a.m}-${round}`} className="flex min-w-0 items-center gap-1.5 text-[12px] leading-5">
            <span className="shrink-0 text-muted-foreground">→ asked <span className="font-medium text-foreground-secondary">{a.m}</span> {a.asks.length} question{a.asks.length === 1 ? '' : 's'}</span>
            <span className="min-w-0 truncate italic text-muted-foreground" title={a.asks[0].text}>“{a.asks[0].text}”</span>
          </div>
        ))}
        {asked.length > 5 && <div className="text-[12px] leading-5 text-muted-foreground">+{asked.length - 5} more members asked</div>}
        {plain.length > 0 && (
          <div className="truncate text-[12px] leading-5 text-muted-foreground">→ sent to {plain.join(', ')}</div>
        )}
      </button>
    )
  }

  return (
    <div data-card={k} data-expanded className={frame} style={style}>
      <button type="button" aria-expanded onClick={onToggle} aria-label={`${huddle.leader}, round ${round} — collapse`} className="block w-full text-left">
        {head}
      </button>
      <div className="mt-2 space-y-3">
        {asks.map((a) => (
          <section key={a.m} className="space-y-1">
            <h4 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              To {a.m}{a.asks.length ? ` — ${a.asks.length} question${a.asks.length === 1 ? '' : 's'}` : ''}
            </h4>
            {a.asks.length > 0 ? <AskList asks={a.asks} /> : (
              <p className="text-[12px] text-muted-foreground">{round === 1 ? 'The round’s ask, with ' + huddle.leader + '’s survey of them.' : 'No questions of its own.'}</p>
            )}
            {a.cell && <Footer cell={{ ...a.cell, has_transcript: false }} />}
          </section>
        ))}
        {asks.length === 0 && <p className="text-[12px] text-muted-foreground">Not started.</p>}
      </div>
    </div>
  )
}

type Drawn = { key: string; d: string; state: ArcState; label: string; x0: number; y0: number; ends: [string, string] }
type Exchange = { key: string; d: string; kind: 'ask' | 'reply' }

/** All the lines, measured from the DOM (`data-anchor` rows, `data-card`
 * cards, `data-cell` grid cells) — re-measured whenever the grid changes size
 * (a card expanding or collapsing, the window resizing, new data).
 *
 * Nothing runs across text: the exchange arrows keep to the gutters between
 * rows; the co-sign arcs leave each card at its edge, level with the row, and
 * run BENEATH the (opaque) cards in between. Pointing at a proposal or an
 * answer redraws just its arcs in a layer ABOVE the cards, end to end. */
function FlowLayer({ arcs, host, focus, rounds, members, showArcs, version }: {
  arcs: Arc[]
  host: React.RefObject<HTMLDivElement | null>
  focus: string | null
  rounds: number[]
  members: string[]
  showArcs: boolean
  /** Bumps when cards expand / collapse. */
  version: string
}) {
  const [drawn, setDrawn] = useState<Drawn[]>([])
  const [exchanges, setExchanges] = useState<Exchange[]>([])
  const [box, setBox] = useState({ w: 0, h: 0 })

  const measure = useCallback(() => {
    const el = host.current
    if (!el) return
    const base = el.getBoundingClientRect()
    const rect = (n: Element | null | undefined): Rect | null => {
      if (!n) return null
      const r = n.getBoundingClientRect()
      return r.width === 0 && r.height === 0 ? null : { x: r.left - base.left, y: r.top - base.top, w: r.width, h: r.height }
    }
    // One pass per attribute (titles are free text, so no attribute selectors).
    const index = (attr: string) => {
      const m = new Map<string, Element>()
      el.querySelectorAll(`[${attr}]`).forEach((n) => m.set(n.getAttribute(attr) ?? '', n))
      return m
    }
    const anchors = index('data-anchor')
    const cards = index('data-card')
    const cells = index('data-cell')
    const cardOf = (n: Element) => n.closest('[data-card]') ?? n.querySelector('[data-card]') ?? n

    const out: Drawn[] = []
    for (const a of arcs) {
      const ends: [string, string] = [anchorKey.answer(a.partner, a.title), anchorKey.proposal(a.lead, a.title)]
      const fromEl = anchors.get(ends[0]) ?? anchors.get(a.from)
      const toEl = anchors.get(ends[1]) ?? anchors.get(a.to)
      if (!fromEl || !toEl) continue
      const fr = rect(fromEl), fc = rect(cardOf(fromEl)), tr = rect(toEl), tc = rect(cardOf(toEl))
      if (!fr || !fc || !tr || !tc) continue
      const { d, start } = flowArc(fr, fc, tr, tc)
      out.push({ key: a.key, d, state: a.state, label: `${a.partner} → ${a.lead}: ${a.title} — ${ARC_LABEL[a.state]}`, x0: start.x, y0: start.y, ends })
    }

    const ex: Exchange[] = []
    for (const r of rounds) {
      const lc = rect(cards.get(cardKey.leader(r)))
      const lcell = rect(cells.get(cardKey.leader(r)))
      if (!lc || !lcell) continue
      const askBus = lcell.y - 12
      const replyBus = lcell.y + lcell.h + 12
      for (const m of members) {
        const node = cards.get(cardKey.member(m, r))
        const mc = rect(node)
        if (!mc) continue
        ex.push({ key: `ask-${m}-${r}`, d: askPath(lc, mc, askBus), kind: 'ask' })
        if (node?.getAttribute('data-cell-state') === 'replied') {
          ex.push({ key: `reply-${m}-${r}`, d: replyPath(mc, lc, replyBus), kind: 'reply' })
        }
      }
    }
    setBox({ w: el.scrollWidth, h: el.scrollHeight })
    setDrawn(out)
    setExchanges(ex)
  }, [arcs, host, rounds, members])

  useLayoutEffect(() => {
    measure()
    // Late layout the grid's own size does not report: web fonts settling,
    // a lazy transcript chunk, a card's content wrapping differently.
    const raf = requestAnimationFrame(measure)
    const late = setTimeout(measure, 300)
    void document.fonts?.ready.then(measure)
    const el = host.current
    const ro = el && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => measure()) : null
    if (el && ro) ro.observe(el)
    return () => {
      cancelAnimationFrame(raf)
      clearTimeout(late)
      ro?.disconnect()
    }
  }, [measure, host, version])

  const arcsShown = showArcs ? drawn : []
  const lit = focus === null ? [] : arcsShown.filter((a) => a.ends.includes(focus))
  return (
    <>
      <svg aria-hidden data-arc-layer="exchange" className="pointer-events-none absolute left-0 top-0 z-0 overflow-visible" width={box.w} height={box.h}>
        <defs>
          <marker id="huddle-exchange-head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--muted-foreground)" />
          </marker>
        </defs>
        {exchanges.map((x) => (
          <path key={x.key} data-exchange={x.kind} d={x.d} fill="none" stroke="var(--muted-foreground)"
            strokeWidth={1.25} strokeLinejoin="round" strokeDasharray={x.kind === 'reply' ? '3 3' : undefined}
            opacity={0.45} markerEnd="url(#huddle-exchange-head)" />
        ))}
      </svg>
      {arcsShown.length > 0 && (
        <ArcSvg drawn={arcsShown} box={box} layer="under" opacity={(a) => (focus === null ? 0.85 : lit.includes(a) ? 0 : 0.25)} />
      )}
      {lit.length > 0 && <ArcSvg drawn={lit} box={box} layer="over" opacity={() => 1} />}
    </>
  )
}

function ArcSvg({ drawn, box, layer, opacity }: {
  drawn: Drawn[]
  box: { w: number; h: number }
  layer: 'under' | 'over'
  opacity: (a: Drawn) => number
}) {
  const over = layer === 'over'
  return (
    <svg
      aria-hidden
      data-arc-layer={layer}
      className={'pointer-events-none absolute left-0 top-0 overflow-visible ' + (over ? 'z-30' : 'z-0')}
      width={box.w}
      height={box.h}
    >
      <defs>
        {(Object.keys(ARC_COLOR) as ArcState[]).map((s) => (
          <marker key={s} id={`huddle-arrow-${layer}-${s}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill={ARC_COLOR[s]} />
          </marker>
        ))}
      </defs>
      {drawn.map((a) => (
        <g key={a.key} data-arc={over ? undefined : a.state} style={{ opacity: opacity(a), transition: 'opacity 150ms ease' }}>
          <title>{a.label}</title>
          <path
            d={a.d}
            fill="none"
            stroke={ARC_COLOR[a.state]}
            strokeWidth={over ? 2.75 : 2}
            strokeLinecap="round"
            strokeDasharray={a.state === 'pending' ? '5 5' : undefined}
            markerEnd={`url(#huddle-arrow-${layer}-${a.state})`}
          />
          <circle cx={a.x0} cy={a.y0} r={over ? 4.5 : 3.5} fill={ARC_COLOR[a.state]} />
        </g>
      ))}
    </svg>
  )
}

export function ArcLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
      <span className="inline-flex items-center gap-1.5">
        <svg width="22" height="6" aria-hidden><line x1="1" y1="3" x2="21" y2="3" stroke="var(--muted-foreground)" strokeOpacity="0.6" strokeWidth="1.25" /></svg>
        ask
      </span>
      <span className="inline-flex items-center gap-1.5">
        <svg width="22" height="6" aria-hidden><line x1="1" y1="3" x2="21" y2="3" stroke="var(--muted-foreground)" strokeOpacity="0.6" strokeWidth="1.25" strokeDasharray="3 3" /></svg>
        reply
      </span>
      {(Object.keys(ARC_COLOR) as ArcState[]).map((s) => (
        <span key={s} className="inline-flex items-center gap-1.5">
          <svg width="22" height="6" aria-hidden>
            <line x1="1" y1="3" x2="21" y2="3" stroke={ARC_COLOR[s]} strokeWidth="2" strokeLinecap="round"
              strokeDasharray={s === 'pending' ? '4 3' : undefined} />
          </svg>
          {ARC_LABEL[s]}
        </span>
      ))}
    </div>
  )
}

function Placeholder({ text }: { text: string }) {
  return (
    <div className="flex h-12 items-center justify-center rounded-xl border border-dashed border-border/60 text-[11px] text-foreground-subtle">
      {text}
    </div>
  )
}

export function HuddleGrid({ huddle, showArcs = true }: { huddle: Huddle; showArcs?: boolean }) {
  const host = useRef<HTMLDivElement | null>(null)
  const phone = useIsPhone()
  const [focus, setFocus] = useState<string | null>(null)
  const [open, setOpen] = useState<Set<string>>(() => new Set())
  const cols = useMemo(() => columns(huddle), [huddle])
  const rounds = useMemo(() => roundsToShow(huddle), [huddle])
  const arcs = useMemo(() => arcsFor(huddle), [huddle])
  const allKeys = useMemo(
    () => rounds.flatMap((r) => [cardKey.leader(r), ...cols.filter((m) => cellAt(huddle, m, r)).map((m) => cardKey.member(m, r))]),
    [rounds, cols, huddle],
  )
  const allOpen = allKeys.length > 0 && allKeys.every((k) => open.has(k))
  const toggle = (k: string) => setOpen((s) => {
    const n = new Set(s)
    if (n.has(k)) n.delete(k)
    else n.add(k)
    return n
  })

  const leaderCard = (r: number) => (
    <LeaderCard huddle={huddle} round={r} members={cols} expanded={open.has(cardKey.leader(r))} onToggle={() => toggle(cardKey.leader(r))} />
  )
  const memberCell = (m: string, i: number, r: number) => {
    const c = cellAt(huddle, m, r)
    return c ? (
      <MemberCard cell={c} hue={memberHue(i)} leader={huddle.leader} arcs={arcs}
        expanded={open.has(cardKey.member(m, r))} onToggle={() => toggle(cardKey.member(m, r))} />
    ) : (
      <Placeholder text={r > huddle.rounds_dispatched ? 'not started' : `not sent to ${m}`} />
    )
  }

  const toolbar = (
    <div className="mb-3 flex items-center gap-3">
      <button type="button" data-expand-all onClick={() => setOpen(allOpen ? new Set() : new Set(allKeys))}
        className="rounded-lg border border-border bg-card px-2.5 py-1 text-[12px] font-medium text-foreground hover:border-primary/50 hover:text-primary">
        {allOpen ? 'Collapse all' : 'Expand all'}
      </button>
      <span className="text-[11px] text-muted-foreground">Click a card to read the whole reply.</span>
    </div>
  )

  if (phone) {
    return (
      <div data-layout="phone">
        {toolbar}
        {rounds.map((r) => (
          <section key={r} data-round={r} className="mb-6 space-y-2">
            {leaderCard(r)}
            <div className="space-y-2 border-l-2 border-border/70 pl-3">
              {cols.map((m, i) => <div key={m} data-cell={cardKey.member(m, r)}>{memberCell(m, i, r)}</div>)}
            </div>
          </section>
        ))}
      </div>
    )
  }

  return (
    <div data-layout="grid">
      {toolbar}
      <div className="-mx-1 overflow-x-auto px-1 pb-2">
        <div
          ref={host}
          onMouseOver={(e) => {
            const hit = (e.target as Element).closest?.('[data-anchor^="prop|"], [data-anchor^="ans|"]')
            setFocus(hit?.getAttribute('data-anchor') ?? null)
          }}
          onMouseLeave={() => setFocus(null)}
          className="relative grid items-start gap-x-6 gap-y-9 pt-1"
          style={{
            gridTemplateColumns: `repeat(${cols.length + 1}, minmax(220px, 1fr))`,
            minWidth: (cols.length + 1) * 244,
          }}
        >
          {/* header row: the leader, then the members */}
          <div className="relative z-10 flex items-center gap-2.5 rounded-xl border border-border bg-muted px-3 py-2"
            style={{ borderTopWidth: 3, borderTopColor: LEADER_HUE }}>
            <MemberAvatar slug={huddle.leader} hue={LEADER_HUE} />
            <div className="min-w-0">
              <div className="truncate text-[14px] font-semibold text-foreground">{huddle.leader}</div>
              <div className="text-[11px] text-muted-foreground">leads · asks each round</div>
            </div>
          </div>
          {cols.map((m, i) => (
            <div
              key={m}
              data-anchor={`head-${m}`}
              className="relative z-10 flex items-center gap-2.5 rounded-xl border border-border bg-card px-3 py-2"
              style={{ borderTopWidth: 3, borderTopColor: memberHue(i) }}
            >
              <MemberAvatar slug={m} hue={memberHue(i)} />
              <div className="min-w-0">
                <div className="truncate text-[14px] font-semibold text-foreground">{m}</div>
                <div className="text-[11px] text-muted-foreground">
                  {`${huddle.cells.filter((c) => c.member === m && c.block).length} of ${rounds.length} rounds replied`}
                </div>
              </div>
            </div>
          ))}

          {rounds.map((r) => [
            <div key={`leader-${r}`} data-cell={cardKey.leader(r)} className="relative z-10 min-w-0 self-stretch">
              {leaderCard(r)}
            </div>,
            ...cols.map((m, i) => (
              <div key={`${m}-${r}`} data-anchor={`${m}-${r}`} data-cell={`${m}-${r}`} className="relative z-10 min-w-0">
                {memberCell(m, i, r)}
              </div>
            )),
          ])}
          <FlowLayer arcs={arcs} host={host} focus={focus} rounds={rounds} members={cols} showArcs={showArcs}
            version={[...open].sort().join(',')} />
        </div>
      </div>
    </div>
  )
}
