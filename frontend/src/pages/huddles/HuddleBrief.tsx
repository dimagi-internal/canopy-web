import { briefItems, briefNotNow } from './briefWords'

/** "Jonathan's priorities for this huddle" — the numbered brief every agent was
 * given, so "Serves priority 2" further down has something to point at. Nothing
 * for a huddle planned without one. */
export function PrioritiesBrief({ brief }: { brief: string }) {
  const items = briefItems(brief)
  if (!items.length) return null
  const notNow = briefNotNow(brief)
  return (
    <section data-brief aria-label="Jonathan's priorities for this huddle" className="mt-4 border-t border-border/70 pt-3">
      <h2 className="text-[12px] font-semibold text-foreground">Jonathan&apos;s priorities for this huddle</h2>
      <ol className="mt-1.5 space-y-1 text-[13px] leading-snug">
        {items.map((i) => (
          <li key={i.n} data-priority={i.n} className="flex gap-2">
            <span className="w-4 shrink-0 text-right font-semibold text-foreground-secondary">{i.n}</span>
            <span className="min-w-0">
              <span className="text-foreground">{i.text}</span>
              {i.dates && i.dates.toLowerCase() !== 'none' && <span className="text-muted-foreground"> · by {i.dates}</span>}
            </span>
          </li>
        ))}
      </ol>
      {notNow && <p className="mt-1.5 text-[12px] text-muted-foreground">Not now: {notNow}</p>}
    </section>
  )
}
