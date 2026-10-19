import { useEffect, useRef, useState } from 'react'
import { Search } from 'lucide-react'

import { previewMySearch } from '../../api/people'
import type { SearchPreview as Preview, SearchPreviewEntry } from '../../api/people'

/** "How agents find it": type a question the way an agent would and see every
 *  entry scored — a bar for closeness in meaning, a line for the cutoff, and why
 *  each would or would not come back. The cutoff slider previews another value
 *  only; agents keep the live one (HCP_SEMANTIC_FLOOR). */
export function SearchPreview() {
  const [query, setQuery] = useState('')
  const [asked, setAsked] = useState('')
  const [result, setResult] = useState<Preview | null>(null)
  const [floor, setFloor] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<number | undefined>(undefined)

  const run = async (q: string, f: number | null) => {
    setBusy(true)
    setError(null)
    try {
      setResult(await previewMySearch(q, f ?? undefined))
      setAsked(q)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  // Re-score when the slider settles, not on every pixel.
  useEffect(() => () => window.clearTimeout(timer.current), [])
  const slide = (v: number) => {
    setFloor(v)
    window.clearTimeout(timer.current)
    if (asked) timer.current = window.setTimeout(() => void run(asked, v), 350)
  }

  const live = result?.live_floor ?? null
  const shown = floor ?? live
  const previewing = floor !== null && live !== null && Math.abs(floor - live) > 0.0001
  const hybrid = result?.method === 'hybrid'
  const back = result?.entries.filter((e) => e.position !== null).length ?? 0

  return (
    <div aria-label="Try a question" className="mt-6">
      <h3 className="text-sm font-semibold text-foreground">Try a question</h3>
      <p className="text-sm text-muted-foreground">
        Type a question the way an agent would to see how it would score today. This changes nothing and isn’t shown to
        anyone.
      </p>
      <form
        className="mt-3 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (query.trim()) void run(query.trim(), floor)
        }}
      >
        <input
          aria-label="Question"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="e.g. how does Jonathan like updates?"
          maxLength={500}
          className="min-w-0 flex-1 rounded-md border border-border bg-background px-2.5 py-1.5 text-sm text-foreground"
        />
        <button
          type="submit"
          disabled={busy || !query.trim()}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          <Search className="h-3.5 w-3.5" /> {busy ? 'Scoring…' : 'Try it'}
        </button>
      </form>
      {error && (
        <p role="alert" className="mt-2 text-sm text-destructive">
          {error}
        </p>
      )}

      {result && (
        <div className={`mt-4 rounded-xl border border-border bg-card p-4 ${busy ? 'opacity-60' : ''}`}>
          <p className="text-sm text-foreground">
            <span className="font-medium">{back === 0 ? 'Nothing' : `${back} of ${result.entries.length}`}</span>{' '}
            {back === 0 ? 'would come back' : `entries would come back`} for “{asked}”.
          </p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {hybrid
              ? `Matched by words and by meaning (${result.model.replace(/^vertex\//, '')}${result.embed_ms != null ? `, ${result.embed_ms} ms` : ''}).`
              : 'Matched by shared words only — search by meaning is off.'}{' '}
            Score = 1 per shared word{hybrid ? ` + ${result.weight} × meaning, once meaning reaches the cutoff` : ''}. Corrections always
            come first. At most {result.limit} come back.
          </p>

          {hybrid && shown !== null && (
            <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <label htmlFor="cutoff">Cutoff</label>
              <input
                id="cutoff"
                type="range"
                min={0.3}
                max={0.9}
                step={0.01}
                value={shown}
                onChange={(e) => slide(Number(e.target.value))}
                className="w-40 accent-primary"
              />
              <span className="tabular-nums text-foreground">{shown.toFixed(2)}</span>
              {previewing ? (
                <>
                  <span className="text-amber-600 dark:text-amber-400">
                    Preview only — agents still use {live!.toFixed(2)} (HCP_SEMANTIC_FLOOR)
                  </span>
                  <button type="button" className="text-primary hover:underline" onClick={() => slide(live!)}>
                    Reset
                  </button>
                </>
              ) : (
                <span>the live setting</span>
              )}
            </div>
          )}

          <ol className="mt-4 space-y-3">
            {result.entries.map((e) => (
              <EntryScore key={e.entry_id ?? e.statement} e={e} cutoff={hybrid ? result.floor : null} />
            ))}
          </ol>

          {hybrid && (
            <p className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span className="inline-flex items-center gap-1.5">
                <span className="inline-block h-2 w-5 rounded-sm bg-emerald-500" /> meaning counts
              </span>
              <span className="inline-flex items-center gap-1.5">
                <span className="inline-block h-2 w-5 rounded-sm bg-muted-foreground/40" /> under the cutoff
              </span>
              <span className="inline-flex items-center gap-1.5">
                <span className="inline-block h-3 w-0.5 bg-foreground" /> cutoff
              </span>
              <span>Bar = how close in meaning, 0 to 1.</span>
            </p>
          )}
        </div>
      )}
    </div>
  )
}

/** One scored entry. `scored` false: a search from before scores were kept —
 *  its place and recorded reason only. */
export function EntryScore({ e, cutoff, scored = true }: { e: SearchPreviewEntry; cutoff: number | null; scored?: boolean }) {
  const counts = e.meaning !== null && cutoff !== null && e.meaning >= cutoff
  const tags = [
    e.kind === 'correction' ? 'correction' : null,
    e.kind === 'role' ? 'role' : null,
    e.project ? `project ${e.project}` : null,
  ].filter(Boolean) as string[]
  return (
    <li className={`flex gap-3 ${e.position === null ? 'opacity-60' : ''}`}>
      <span
        className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-medium ${
          e.position !== null ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'
        }`}
        title={e.position !== null ? `Would come back #${e.position}` : 'Would not come back'}
      >
        {e.position ?? '–'}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-sm text-foreground">
          {e.statement ?? <span className="italic text-muted-foreground">(an entry since deleted)</span>}
          {tags.map((t) => (
            <span key={t} className="ml-1.5 rounded bg-muted px-1.5 py-0.5 align-middle text-[10px] text-muted-foreground">
              {t}
            </span>
          ))}
        </p>
        {e.meaning !== null && cutoff !== null && (
          <div className="mt-1.5 flex items-center gap-2">
            <div className="relative h-2 flex-1 rounded-full bg-muted" aria-hidden>
              <div
                className={`h-2 rounded-full ${counts ? 'bg-emerald-500' : 'bg-muted-foreground/40'}`}
                style={{ width: `${Math.max(0, Math.min(1, e.meaning)) * 100}%` }}
              />
              <div className="absolute -top-1 h-4 w-0.5 bg-foreground" style={{ left: `${cutoff * 100}%` }} />
            </div>
            <span className="w-10 text-right text-xs tabular-nums text-muted-foreground">{e.meaning.toFixed(2)}</span>
          </div>
        )}
        <p className="mt-1 text-xs text-muted-foreground">
          {e.position !== null ? 'Why: ' : 'Left out: '}
          {e.because || (scored ? '—' : 'not recorded then')}
          {scored && e.position !== null && <span className="tabular-nums"> · score {e.total.toFixed(2)}</span>}
        </p>
      </div>
    </li>
  )
}
