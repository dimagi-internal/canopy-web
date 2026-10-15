import type { Divergence, Storyboard } from '@/api/storyboards'
import { fmtDuration } from '@/components/ddd/CutPlayer'

/**
 * The framing a set of demos needs and a title + lede cannot carry: who it is
 * for, the point to land, what is true about the data on screen, the order to
 * play it in and the short version. The set's assumptions and open questions live in the decision
 * log (DecisionLog), not here. All of it is optional; a board
 * without a brief renders exactly as before.
 *
 * This existed as a Google Doc beside the board ("Connect Supply demos: scripts
 * and recorded cuts"), because the board could not hold it. The board is the
 * one link a reviewer is sent, so it belongs here.
 */

/** A stable DOM id for one cut on the board, so the running order can link to it. */
export function cutDomId(narrativeSlug: string, cutId: string): string {
  return `cut-${narrativeSlug}-${cutId}`
}

export interface RunItem {
  key: string
  title: string
  /** Which narrative it belongs to — shown when the set has more than one. */
  part: string
  duration: number | null
  href: string
  optional: boolean
}

/** Every cut on the board, in arc order. A narrative without cuts is one item. */
export function runningOrder(board: Storyboard): RunItem[] {
  const items: RunItem[] = []
  for (const act of board.acts) {
    for (const entry of act.entries) {
      const cuts = entry.cuts ?? []
      if (cuts.length === 0) {
        items.push({
          key: entry.narrative_slug,
          title: entry.title,
          part: entry.title,
          duration: null,
          href: `#entry-${entry.narrative_slug}`,
          optional: false,
        })
        continue
      }
      for (const c of cuts) {
        items.push({
          key: `${entry.narrative_slug}/${c.cut_id}`,
          title: c.title,
          part: entry.title,
          duration: c.duration_sec,
          href: `#${cutDomId(entry.narrative_slug, c.cut_id)}`,
          optional: c.optional,
        })
      }
    }
  }
  return items
}

/** Total of the known lengths, and whether any were unknown. */
export function totalOf(items: RunItem[]): { sec: number; partial: boolean } {
  let sec = 0
  let partial = false
  for (const i of items) {
    if (i.duration) sec += i.duration
    else partial = true
  }
  return { sec, partial }
}

function totalLabel(items: RunItem[]): string | null {
  const { sec, partial } = totalOf(items)
  const t = fmtDuration(sec)
  if (!t) return null
  return partial ? `at least ${t}` : t
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-0.5 sm:grid-cols-[9.5rem_1fr] sm:gap-3">
      <dt className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground sm:pt-0.5">
        {label}
      </dt>
      <dd className="text-[14.5px] leading-relaxed text-foreground">{children}</dd>
    </div>
  )
}

function OrderList({ items, multiPart }: { items: RunItem[]; multiPart: boolean }) {
  return (
    <ol className="space-y-1">
      {items.map((it, i) => (
        <li key={it.key} className="flex items-baseline gap-2 text-[13.5px]">
          <span className="w-5 shrink-0 text-right tabular-nums text-muted-foreground">{i + 1}.</span>
          <a href={it.href} className="min-w-0 text-foreground hover:text-primary hover:underline">
            {it.title}
            {multiPart && it.part !== it.title && (
              <span className="text-muted-foreground"> · {it.part}</span>
            )}
            {it.optional && <span className="text-muted-foreground"> (optional)</span>}
          </a>
          <span className="ml-auto shrink-0 tabular-nums text-muted-foreground">
            {fmtDuration(it.duration) ?? ''}
          </span>
        </li>
      ))}
    </ol>
  )
}

export function BriefPanel({ board }: { board: Storyboard }) {
  const brief = board.brief ?? {}
  const order = runningOrder(board)
  const cutCount = order.filter((i) => i.href.startsWith('#cut-')).length
  const multiPart = board.acts.reduce((n, a) => n + a.entries.length, 0) > 1

  const short = brief.short_version
  // A ref without a cut id means the whole narrative: every cut of it.
  const shortItems: RunItem[] = (short?.items ?? []).flatMap((ref) =>
    order.filter((o) =>
      ref.cut_id
        ? o.key === `${ref.narrative_slug}/${ref.cut_id}`
        : o.key === ref.narrative_slug || o.key.startsWith(`${ref.narrative_slug}/`),
    ),
  )

  const hasFraming = !!(brief.audience || brief.purpose || brief.data_disclosure)
  // A running order of one item says nothing the page below does not.
  const showOrder = cutCount > 1 || order.length > 1
  if (!hasFraming && !showOrder && shortItems.length === 0) return null

  const total = totalLabel(order)

  return (
    <section aria-label="About this set" className="mt-8 rounded-lg border border-border bg-card p-4 sm:p-5">
      {hasFraming && (
        <dl className="space-y-3">
          {brief.audience && <Row label="For">{brief.audience}</Row>}
          {brief.purpose && <Row label="The point to land">{brief.purpose}</Row>}
          {brief.data_disclosure && (
            <Row label="About the data">
              <span className="block rounded-md border border-info/30 bg-info/10 px-3 py-2 text-[13.5px] text-foreground">
                {brief.data_disclosure}
              </span>
            </Row>
          )}
        </dl>
      )}

      {showOrder && (
        <div className={hasFraming ? 'mt-5 border-t border-border pt-4' : ''}>
          <h2 className="mb-2 flex items-baseline gap-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            Running order
            {total && <span className="font-normal normal-case tracking-normal">· {total} in all</span>}
          </h2>
          <OrderList items={order} multiPart={multiPart} />
        </div>
      )}

      {shortItems.length > 0 && (
        <div className="mt-5 border-t border-border pt-4">
          <h2 className="mb-1 flex items-baseline gap-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            {short?.label || 'Short version'}
            {totalLabel(shortItems) && (
              <span className="font-normal normal-case tracking-normal">· {totalLabel(shortItems)}</span>
            )}
          </h2>
          {short?.note && <p className="mb-2 text-[13px] text-foreground-secondary">{short.note}</p>}
          <OrderList items={shortItems} multiPart={multiPart} />
        </div>
      )}
    </section>
  )
}

export type Tagged<T> = { item: T; from: string | null }

function From({ from }: { from: string | null }) {
  return from ? <span className="text-muted-foreground"> · {from}</span> : null
}

/** The caveats section for one narrative's review page. */
export function CaveatsView({
  assumptions,
  divergences,
  questions,
}: {
  assumptions: Tagged<string>[]
  divergences: Tagged<Divergence>[]
  questions: Tagged<string>[]
}) {
  if (!assumptions.length && !divergences.length && !questions.length) return null
  return (
    <section aria-label="Caveats" className="mt-14 flex flex-col gap-6">
      <div className="border-b border-border pb-3">
        <h2 className="text-xl font-semibold leading-tight tracking-tight text-foreground">
          Before you share this
        </h2>
        <p className="mt-1.5 text-sm text-foreground-secondary">
          What the demos assume, where the script and the data do not yet agree, and what is still open.
        </p>
      </div>

      {divergences.length > 0 && (
        <div>
          <h3 className="mb-2 text-[13px] font-semibold text-foreground">Where the script and the data disagree</h3>
          <ul className="flex flex-col gap-2">
            {divergences.map(({ item, from }, i) => (
              <li key={i} className="rounded-md border border-warning/30 bg-warning/5 p-3 text-[13.5px]">
                <p className="text-foreground">
                  <span className="text-muted-foreground">The script says: </span>
                  {item.script}
                  <From from={from} />
                </p>
                {item.data && (
                  <p className="mt-1 text-foreground">
                    <span className="text-muted-foreground">The data shows: </span>
                    {item.data}
                  </p>
                )}
                {item.resolution && (
                  <p className="mt-1 text-foreground-secondary">
                    <span className="text-muted-foreground">What we will do: </span>
                    {item.resolution}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {assumptions.length > 0 && (
        <div>
          <h3 className="mb-2 text-[13px] font-semibold text-foreground">Assumptions</h3>
          <ul className="list-disc space-y-1 pl-5 text-[13.5px] text-foreground-secondary">
            {assumptions.map(({ item, from }, i) => (
              <li key={i}>
                {item}
                <From from={from} />
              </li>
            ))}
          </ul>
        </div>
      )}

      {questions.length > 0 && (
        <div>
          <h3 className="mb-2 text-[13px] font-semibold text-foreground">Open questions</h3>
          <ul className="list-disc space-y-1 pl-5 text-[13.5px] text-foreground-secondary">
            {questions.map(({ item, from }, i) => (
              <li key={i}>
                {item}
                <From from={from} />
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

