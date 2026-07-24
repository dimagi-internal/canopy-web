import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { listNarratives, type DddNarrativeListItem } from '@/api/ddd'

/** "nutrition-demo" -> "Nutrition Demo". The slug is the stable identity; this
 *  is just a friendlier headline than the raw kebab-case. */
function humanize(slug: string): string {
  return slug.replace(/[-_]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    })
  } catch {
    return iso
  }
}

function NarrativeCard({ n }: { n: DddNarrativeListItem }) {
  return (
    <Link
      to={`/ddd/${encodeURIComponent(n.slug)}`}
      className="group flex flex-col gap-2 rounded-xl border border-border bg-card p-4 transition-colors hover:border-input"
    >
      <div className="flex items-start justify-between gap-2">
        <h3 className="min-w-0 truncate text-sm font-semibold text-foreground group-hover:text-primary">
          {humanize(n.slug)}
        </h3>
        <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-foreground-secondary">
          {n.run_count} run{n.run_count === 1 ? '' : 's'}
        </span>
      </div>
      <div className="font-mono text-[10px] text-muted-foreground">{n.slug}</div>
      {n.title && (
        <p className="line-clamp-2 text-xs leading-relaxed text-foreground-secondary">{n.title}</p>
      )}
      <div className="mt-auto flex flex-wrap items-center gap-2 pt-1 text-[11px] text-muted-foreground">
        {n.phase && (
          <span className="rounded border border-input bg-muted/60 px-1.5 py-0.5 text-foreground-secondary">
            {n.phase}
          </span>
        )}
        {n.has_video && <span title="has video">Video</span>}
        {n.has_deck && <span title="has deck">Deck</span>}
        <span className="ml-auto">{fmtDate(n.latest_at)}</span>
      </div>
    </Link>
  )
}

/**
 * The DDD index main pane: a browsable gallery of every narrative, so the
 * landing surface shows the work instead of an empty "pick one" void. Clicking a
 * card opens that narrative's landing (versions + runs). Read-only; the left rail
 * remains the primary picker, this mirrors it as scannable cards.
 */
export function NarrativeGallery() {
  const [narratives, setNarratives] = useState<DddNarrativeListItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listNarratives({})
      .then((d) => !cancelled && setNarratives(d))
      .catch((e) => !cancelled && setError(String(e.message || e)))
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="mx-auto max-w-5xl px-4 py-6 sm:px-8">
      <header className="mb-5">
        <h1 className="text-2xl font-semibold text-foreground">Narratives</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Every DDD narrative, newest activity first. Open one to see its versions and runs.
        </p>
      </header>

      {error && <div className="text-sm text-destructive/90">Error: {error}</div>}
      {!narratives && !error && <div className="text-sm text-muted-foreground">Loading…</div>}
      {narratives && narratives.length === 0 && (
        <div className="rounded-xl border border-dashed border-border px-4 py-10 text-center text-sm text-muted-foreground">
          No narratives yet.
        </div>
      )}
      {narratives && narratives.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {narratives.map((n) => (
            <NarrativeCard key={n.slug} n={n} />
          ))}
        </div>
      )}
    </div>
  )
}
