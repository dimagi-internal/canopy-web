import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { Huddle, HuddleCell } from '@/api/huddles'
import { TurnTranscript } from '@/components/activity/TurnTranscript'
import { statusToken } from '@/components/activity/turnLog'
import { curve } from '@/pages/topology/topologyMap'
import { BlockView } from './BlockView'
import {
  anchorKey, arcsFor, cellAt, cellState, columns, critiqueFrom, initial, memberHue, roundName, roundsToShow,
  type Answer, type Arc, type Block,
} from './huddleModel'

/**
 * The conversation: one column per member, one row per round. Each cell is the
 * member's reply as a speech bubble, with the leader's critique quoted above
 * the rounds it shaped; joint proposals are tied to each partner's answer by an
 * arc coloured by that answer. Everything is read from the derived API — the
 * page holds no huddle state of its own.
 */

const ARC_COLOR: Record<Answer, string> = {
  'co-sign': 'var(--success)',
  amend: 'var(--warning)',
  decline: 'var(--destructive)',
  pending: 'var(--muted-foreground)',
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

function Bubble({ cell, hue, leader, arcs }: { cell: HuddleCell; hue: string; leader: string; arcs: Arc[] }) {
  const [open, setOpen] = useState<'' | 'prompt' | 'transcript'>('')
  const state = cellState(cell)
  const critique = cell.round > 1 ? critiqueFrom(cell.prompt, leader) : ''

  return (
    <div className="flex flex-col gap-2">
      {critique && (
        <figure className="ml-6 rounded-2xl rounded-tr-sm border border-border bg-muted/50 px-3 py-2">
          <figcaption className="mb-0.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            {leader} asked
          </figcaption>
          <blockquote className="whitespace-pre-wrap text-[12px] leading-snug text-foreground-secondary">{critique}</blockquote>
        </figure>
      )}
      <div
        data-cell-state={state}
        className={
          'relative mr-4 rounded-2xl rounded-tl-sm border bg-card px-3.5 py-3 shadow-sm ' +
          (state === 'waiting' ? 'border-dashed border-info/50' : 'border-border')
        }
        style={{ borderLeftWidth: 3, borderLeftColor: hue }}
      >
        <div className="mb-2 flex flex-wrap items-center gap-1.5">
          <span className={`inline-flex h-5 items-center rounded-full border px-2 text-[11px] font-medium ${statusToken(cell.status)}`}>
            {cell.status}
          </span>
          {cell.attempt > 1 && <span className="text-[11px] text-muted-foreground">attempt {cell.attempt}</span>}
          {cell.reply_source === 'transcript' && (
            <span className="text-[11px] text-muted-foreground" title="No close-out was filed; read from the turn's transcript">
              from transcript
            </span>
          )}
        </div>

        {state === 'replied' && <BlockView block={cell.block as Block} member={cell.member} arcs={arcs} />}
        {state === 'waiting' && (
          <div className="space-y-2" aria-live="polite">
            <p className="flex items-center gap-2 text-[12px] text-info">
              <span className="relative flex size-2">
                <span className="absolute inline-flex size-full animate-ping rounded-full bg-info/60" />
                <span className="relative inline-flex size-2 rounded-full bg-info" />
              </span>
              Waiting for {cell.member}…
            </p>
            <div className="space-y-1.5" aria-hidden>
              <div className="h-2 w-11/12 animate-pulse rounded bg-muted" />
              <div className="h-2 w-8/12 animate-pulse rounded bg-muted" />
              <div className="h-2 w-9/12 animate-pulse rounded bg-muted" />
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

        {!cell.content_hidden && (
          <div className="mt-3 flex gap-3 border-t border-border/60 pt-2 text-[11px]">
            {cell.prompt && (
              <button
                type="button"
                onClick={() => setOpen(open === 'prompt' ? '' : 'prompt')}
                className="text-muted-foreground hover:text-foreground"
                aria-expanded={open === 'prompt'}
              >
                {open === 'prompt' ? 'Hide prompt' : 'Prompt'}
              </button>
            )}
            {cell.has_transcript && (
              <button
                type="button"
                onClick={() => setOpen(open === 'transcript' ? '' : 'transcript')}
                className="text-muted-foreground hover:text-foreground"
                aria-expanded={open === 'transcript'}
              >
                {open === 'transcript' ? 'Hide transcript' : 'Transcript'}
              </button>
            )}
          </div>
        )}
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
      </div>
    </div>
  )
}

type Drawn = { key: string; d: string; state: Answer; x0: number; y0: number; ends: [string, string] }

/** The co-sign arcs, laid over the grid. Positions are measured from the DOM
 * (`data-anchor`), so they follow the layout — re-measured whenever the grid
 * changes size (a bubble expanding, the window resizing, new data). */
function ArcLayer({ arcs, host, focus }: {
  arcs: Arc[]
  host: React.RefObject<HTMLDivElement | null>
  /** The `data-anchor` of the proposal / answer under the pointer, if any. */
  focus: string | null
}) {
  const [drawn, setDrawn] = useState<Drawn[]>([])
  const [box, setBox] = useState({ w: 0, h: 0 })

  const measure = useCallback(() => {
    const el = host.current
    if (!el) return
    const base = el.getBoundingClientRect()
    // One pass over the anchors (titles are free text, so no attribute selectors).
    const nodes = new Map<string, Element>()
    el.querySelectorAll('[data-anchor]').forEach((n) => nodes.set(n.getAttribute('data-anchor') ?? '', n))
    const rectOf = (...keys: string[]) => {
      for (const k of keys) {
        const node = nodes.get(k)
        if (node) {
          const r = node.getBoundingClientRect()
          return { x: r.left - base.left, y: r.top - base.top, w: r.width, h: r.height }
        }
      }
      return null
    }
    const out: Drawn[] = []
    for (const a of arcs) {
      const ends: [string, string] = [anchorKey.answer(a.partner, a.title), anchorKey.proposal(a.lead, a.title)]
      const from = rectOf(ends[0], a.from)
      const to = rectOf(ends[1], a.to)
      if (!from || !to || from.w === 0 || to.w === 0) continue
      const c = curve(from, to)
      if (!c) continue
      const m = /^M ([\d.-]+) ([\d.-]+)/.exec(c.d)
      out.push({ key: a.key, d: c.d, state: a.state, x0: Number(m?.[1] ?? 0), y0: Number(m?.[2] ?? 0), ends })
    }
    setBox({ w: el.scrollWidth, h: el.scrollHeight })
    setDrawn(out)
  }, [arcs, host])

  useLayoutEffect(() => {
    measure()
    // Late layout the grid's own size does not report: web fonts settling,
    // a lazy transcript chunk, a bubble's content wrapping differently.
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
  }, [measure, host])

  if (drawn.length === 0) return null
  return (
    <svg
      aria-hidden
      className="pointer-events-none absolute left-0 top-0 z-20 overflow-visible"
      width={box.w}
      height={box.h}
    >
      <defs>
        {(Object.keys(ARC_COLOR) as Answer[]).map((s) => (
          <marker key={s} id={`huddle-arrow-${s}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill={ARC_COLOR[s]} />
          </marker>
        ))}
      </defs>
      {drawn.map((a) => {
        // At rest every arc is quiet enough to read through; pointing at a
        // proposal or an answer lifts its arcs and fades the rest.
        const lit = focus !== null && a.ends.includes(focus)
        const opacity = focus === null ? 0.55 : lit ? 1 : 0.1
        return (
          <g key={a.key} data-arc={a.state} style={{ opacity, transition: 'opacity 150ms ease' }}>
            <path
              d={a.d}
              fill="none"
              stroke={ARC_COLOR[a.state]}
              strokeWidth={lit ? 2.75 : 1.75}
              strokeLinecap="round"
              strokeDasharray={a.state === 'pending' ? '5 5' : undefined}
              markerEnd={`url(#huddle-arrow-${a.state})`}
            />
            <circle cx={a.x0} cy={a.y0} r={lit ? 4.5 : 3.5} fill={ARC_COLOR[a.state]} />
          </g>
        )
      })}
    </svg>
  )
}

export function ArcLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
      {(Object.keys(ARC_COLOR) as Answer[]).map((s) => (
        <span key={s} className="inline-flex items-center gap-1.5">
          <svg width="22" height="6" aria-hidden>
            <line x1="1" y1="3" x2="21" y2="3" stroke={ARC_COLOR[s]} strokeWidth="2" strokeLinecap="round"
              strokeDasharray={s === 'pending' ? '4 3' : undefined} />
          </svg>
          {s === 'co-sign' ? 'co-signed' : s === 'amend' ? 'amended' : s === 'decline' ? 'declined' : 'not answered'}
        </span>
      ))}
    </div>
  )
}

export function HuddleGrid({ huddle, showArcs = true }: { huddle: Huddle; showArcs?: boolean }) {
  const host = useRef<HTMLDivElement | null>(null)
  const [focus, setFocus] = useState<string | null>(null)
  const cols = useMemo(() => columns(huddle), [huddle])
  const rounds = useMemo(() => roundsToShow(huddle), [huddle])
  const arcs = useMemo(() => arcsFor(huddle), [huddle])

  return (
    <div className="-mx-1 overflow-x-auto px-1 pb-2">
      <div
        ref={host}
        onMouseOver={(e) => {
          const hit = (e.target as Element).closest?.('[data-anchor^="prop|"], [data-anchor^="ans|"]')
          setFocus(hit?.getAttribute('data-anchor') ?? null)
        }}
        onMouseLeave={() => setFocus(null)}
        className="relative grid gap-x-3 gap-y-5"
        style={{
          gridTemplateColumns: `104px repeat(${cols.length}, minmax(270px, 1fr))`,
          minWidth: 104 + cols.length * 286,
        }}
      >
        {/* header row */}
        <div className="sticky left-0 z-10 bg-background" />
        {cols.map((m, i) => (
          <div
            key={m}
            data-anchor={`head-${m}`}
            className="flex items-center gap-2.5 rounded-xl border border-border bg-card/60 px-3 py-2"
            style={{ borderTopWidth: 3, borderTopColor: memberHue(i) }}
          >
            <MemberAvatar slug={m} hue={memberHue(i)} />
            <div className="min-w-0">
              <div className="truncate text-[14px] font-semibold text-foreground">{m}</div>
              <div className="text-[11px] text-muted-foreground">
                {m === huddle.leader ? 'leads' : `${huddle.cells.filter((c) => c.member === m && c.block).length} of ${rounds.length} rounds replied`}
              </div>
            </div>
          </div>
        ))}

        {rounds.map((r) => {
          const inRound = huddle.cells.filter((c) => c.round === r)
          const live = inRound.some((c) => cellState(c) === 'waiting')
          const replied = inRound.filter((c) => c.block).length
          return [
            <div key={`label-${r}`} className="sticky left-0 z-10 bg-background pr-2 pt-1">
              <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Round {r}</div>
              <div className="mt-0.5 flex items-center gap-1.5 text-[14px] font-semibold text-foreground">
                {roundName(huddle.type, r)}
                {live && <span className="size-1.5 animate-pulse rounded-full bg-info" aria-label="in flight" />}
              </div>
              <div className="mt-0.5 text-[11px] text-muted-foreground">
                {inRound.length === 0 ? 'not started' : `${replied}/${inRound.length} replied`}
              </div>
            </div>,
            ...cols.map((m, i) => {
              const c = cellAt(huddle, m, r)
              return (
                <div key={`${m}-${r}`} data-anchor={`${m}-${r}`} data-cell={`${m}-${r}`} className="min-w-0">
                  {c ? (
                    <Bubble cell={c} hue={memberHue(i)} leader={huddle.leader} arcs={arcs} />
                  ) : (
                    <div className="mr-4 flex h-14 items-center justify-center rounded-2xl border border-dashed border-border/60 text-[11px] text-foreground-subtle">
                      {r > huddle.rounds_dispatched ? 'not started' : `not sent to ${m}`}
                    </div>
                  )}
                </div>
              )
            }),
          ]
        })}
        {showArcs && <ArcLayer arcs={arcs} host={host} focus={focus} />}
      </div>
    </div>
  )
}
