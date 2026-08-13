import { useEffect, useState, type JSX } from 'react'
import { useOutletContext, useSearchParams } from 'react-router-dom'
import { listItems, decideItem, type ItemOut, type ItemDecision } from '@/api/items'
import { Markdown } from '@/components/Markdown'
import { ItemAge } from '@/components/items/ItemAge'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'

// A batch of Items reviewed in one sitting — Ada's fleet audit's home. It belongs
// to the AGENT whose queue it is, NOT to a DDD narrative: the old findings review
// borrowed that surface (because this object did not exist) and conjured a
// phantom narrative doing it.
//
// CLOSED decision set — these three render for any item, including one this UI has
// never seen before. That is the whole point of the vocabulary being closed.
const DECISIONS: ItemDecision[] = ['implement', 'skip', 'defer']

/** Who an item's work goes to. Empty target_agent means self — Ada's fan-out is
 *  the same field naming someone else. */
function dispatchTargets(item: ItemOut): string[] {
  return (item.dispatch ?? []).map(
    (d) => (d as { target_agent?: string }).target_agent || item.agent_slug,
  )
}

function ItemCard({
  item,
  onDecided,
}: {
  item: ItemOut
  onDecided: (i: ItemOut) => void
}): JSX.Element {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [comment, setComment] = useState('')
  const decided = item.state !== 'open'
  const targets = dispatchTargets(item)

  return (
    <article
      data-testid={`item-${item.idempotency_key}`}
      className="rounded-lg border border-border bg-card p-4"
    >
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-sm font-semibold text-foreground">{item.title}</h3>
        {decided && (
          <span
            data-testid="item-state"
            className="shrink-0 rounded bg-muted px-1.5 py-px text-[10px] uppercase tracking-wide text-muted-foreground"
          >
            {item.state}
          </span>
        )}
      </div>

      {/* Same reason as the inbox row: a card with no date on it can't be judged
          stale, and this view mixes open cards with long-decided ones. */}
      <p className="mt-1 text-[11px] leading-snug text-muted-foreground">
        <ItemAge createdAt={item.created_at} decidedAt={item.decided_at} />
      </p>

      {item.body && (
        <Markdown className="mt-2 text-[13px] leading-snug text-foreground-secondary">
          {item.body}
        </Markdown>
      )}

      {targets.length > 0 && (
        <p className="mt-2 text-[11px] text-muted-foreground">
          Implement dispatches to {targets.join(', ')}
        </p>
      )}

      {decided && item.comment && (
        <p className="mt-2 text-[11px] text-muted-foreground">“{item.comment}”</p>
      )}

      {error && <p className="mt-2 text-[11px] text-destructive">{error}</p>}

      {!decided && (
        <>
          <textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="Comment (optional) — what to change, or why skip…"
            className="mt-3 w-full rounded-md border border-input bg-input p-2 text-[13px] text-foreground"
            rows={2}
          />
          <div className="mt-2 flex gap-2">
            {DECISIONS.map((d) => (
              <button
                key={d}
                type="button"
                disabled={busy}
                onClick={async () => {
                  setBusy(true)
                  setError(null)
                  try {
                    onDecided(await decideItem(item.id, d, comment))
                  } catch (e: unknown) {
                    // A bad dispatch spec 422s and the item stays open — say so
                    // rather than leaving the click looking like it worked.
                    setError(e instanceof Error ? e.message : 'Failed to decide')
                  } finally {
                    setBusy(false)
                  }
                }}
                className="rounded-md border border-border px-3 py-1 text-[13px] capitalize text-foreground transition-colors hover:bg-muted disabled:opacity-50"
              >
                {d}
              </button>
            ))}
          </div>
        </>
      )}
    </article>
  )
}

export function ItemsSection(): JSX.Element {
  const { agent } = useOutletContext<AgentOutletContext>()
  const slug = agent.slug
  const [params] = useSearchParams()
  const batch = params.get('batch') ?? ''
  const [items, setItems] = useState<ItemOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setItems(null)
    listItems(slug, batch ? { batch } : {})
      .then((rows) => !cancelled && setItems(rows))
      .catch((e: unknown) =>
        !cancelled && setError(e instanceof Error ? e.message : 'Failed to load'),
      )
    return () => {
      cancelled = true
    }
  }, [slug, batch])

  if (error) return <p className="p-4 text-[13px] text-destructive">{error}</p>
  if (!items) return <p className="p-4 text-[13px] text-muted-foreground">Loading…</p>

  // This surface exists to ask a human for a decision, so what is still UNDECIDED is
  // the whole content and everything else is archive. Items are never deleted — only
  // moved to `decided`/`dismissed` — so rendering every state made the page grow
  // without bound: it reached 32 cards, 30 of them settled, and the 2 that actually
  // wanted an answer were buried in weeks of history. Settled cards now collapse
  // behind a disclosure. A `?batch=` permalink is the deliberate exception: it names
  // one sitting and is usually opened precisely to read back what was decided.
  const open = items.filter((i) => i.state === 'open')
  const settled = items.filter((i) => i.state !== 'open')
  const showAll = Boolean(batch)
  const primary = showAll ? items : open

  const renderCard = (i: ItemOut): JSX.Element => (
    <ItemCard
      key={i.id}
      item={i}
      onDecided={(updated) =>
        setItems((prev) => (prev ?? []).map((p) => (p.id === updated.id ? updated : p)))
      }
    />
  )

  return (
    <div className="flex flex-col gap-3 p-4" data-testid="items-batch">
      <header>
        <h2 className="text-base font-semibold text-foreground">{batch || 'Items'}</h2>
        <p className="text-[11px] text-muted-foreground">
          {showAll
            ? `${items.length} item${items.length === 1 ? '' : 's'} · ${open.length} open`
            : `${open.length} waiting on you`}
        </p>
      </header>
      {primary.length === 0 ? (
        <p className="rounded-lg border border-border bg-card p-3 text-[13px] text-muted-foreground">
          {showAll || settled.length === 0 ? 'Nothing here.' : 'Nothing waiting on you.'}
        </p>
      ) : (
        primary.map(renderCard)
      )}
      {!showAll && settled.length > 0 && (
        <details data-testid="items-settled">
          <summary className="cursor-pointer text-[11px] text-muted-foreground">
            {settled.length} settled
          </summary>
          <div className="mt-3 flex flex-col gap-3">{settled.map(renderCard)}</div>
        </details>
      )}
    </div>
  )
}
