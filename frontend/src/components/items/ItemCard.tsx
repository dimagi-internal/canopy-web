import { useState, type JSX } from 'react'
import { decideItem, dismissItem, type ItemDecision, type ItemOut } from '@/api/items'
import { Markdown } from '@/components/Markdown'
import { ItemAge } from '@/components/items/ItemAge'

// One actionable inbox row, shared by both surfaces that render open items: the
// /supervisor fleet queue and the per-agent rail. Every open Item is decidable in
// place — no bouncing to another surface. A `review` gets implement / skip / defer;
// a `question` is resolved by typing its answer. Both can be dismissed.
//
// Both `implement` and an ANSWER dispatch the row's work immediately (a question has
// no verb to click — the answer is the go-ahead), so the button says what it will do
// when there is something to route. A bad dispatch spec 422s and the item stays OPEN
// — we surface that inline rather than let the tap look like it worked. A second
// decision 409s (already decided elsewhere); we refetch via onActed.

const REVIEW_DECISIONS: ItemDecision[] = ['implement', 'skip', 'defer']

/** Where implementing sends the work — the one place Ada's cross-agent fan-out is
 *  visible in the row itself. Empty target means self. */
function dispatchHint(item: ItemOut): string {
  const targets = (item.dispatch ?? [])
    .map((d) => (d as { target_agent?: string }).target_agent || item.agent_slug)
    .filter((v, i, a) => a.indexOf(v) === i)
  return targets.length ? `dispatches to ${targets.join(', ')}` : ''
}

export function ItemCard({
  item,
  onActed,
  showAgent = false,
}: {
  item: ItemOut
  onActed: () => void
  showAgent?: boolean
}): JSX.Element {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [answer, setAnswer] = useState('')

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      onActed()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Action failed')
      setBusy(false)
    }
  }

  const meta = [showAgent ? item.agent_slug : '', dispatchHint(item)].filter(Boolean).join(' · ')

  return (
    <article
      data-testid={`item-${item.id}`}
      className="rounded-lg border border-border bg-card p-3"
    >
      <p className="text-[13px] font-semibold leading-snug text-foreground">{item.title}</p>
      {/* Age is never conditional — an undecided card with no date on it cannot be
          triaged, which is the whole complaint this row exists to answer. */}
      <p className="mt-1 flex flex-wrap items-center gap-x-1.5 text-[11px] leading-snug text-muted-foreground">
        <ItemAge createdAt={item.created_at} decidedAt={item.decided_at} />
        {meta && <span>· {meta}</span>}
      </p>
      {item.body && (
        <Markdown className="mt-1 text-[12px] leading-snug text-foreground-secondary">
          {item.body}
        </Markdown>
      )}
      {error && <p className="mt-1 text-[11px] text-destructive">{error}</p>}

      {item.kind === 'question' ? (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <input
            type="text"
            value={answer}
            disabled={busy}
            placeholder="Type an answer…"
            onChange={(e) => setAnswer(e.target.value)}
            className="min-w-0 flex-1 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground placeholder:text-foreground-subtle disabled:opacity-50"
            data-testid={`item-answer-${item.id}`}
          />
          <button
            type="button"
            disabled={busy || !answer.trim()}
            onClick={() => act(() => decideItem(item.id, '', answer.trim()))}
            className="rounded-md border border-border px-3 py-1 text-[12px] text-foreground transition-colors hover:bg-muted disabled:opacity-50"
          >
            {(item.dispatch ?? []).length ? 'Answer & run' : 'Answer'}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => act(() => dismissItem(item.id))}
            className="rounded-md px-2 py-1 text-[12px] text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50"
          >
            Dismiss
          </button>
        </div>
      ) : (
        <div className="mt-2 flex flex-wrap gap-2">
          {REVIEW_DECISIONS.map((d) => (
            <button
              key={d}
              type="button"
              disabled={busy}
              onClick={() => act(() => decideItem(item.id, d))}
              className="rounded-md border border-border px-3 py-1 text-[12px] capitalize text-foreground transition-colors hover:bg-muted disabled:opacity-50"
              data-testid={`item-${d}-${item.id}`}
            >
              {d}
            </button>
          ))}
          <button
            type="button"
            disabled={busy}
            onClick={() => act(() => dismissItem(item.id))}
            className="ml-auto rounded-md px-2 py-1 text-[12px] text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50"
          >
            Dismiss
          </button>
        </div>
      )}
    </article>
  )
}

// Group open items into their ranked bands and render them. Shared body for both
// the fleet queue and the rail; the surfaces differ only in whether they tag each
// row with its agent (fleet) and in their header chrome.
export { REVIEW_DECISIONS }
