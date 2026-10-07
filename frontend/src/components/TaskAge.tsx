import type { JSX } from 'react'
import { relativeTime } from '@/components/activity/turnLog'

// How old an Item is — read from ONE place by both card surfaces (the shared inbox
// ItemCard and the batch view's own card), for the same reason the bands are shared:
// two renderers of the same object drift.
//
// Jonathan, 2026-08-12, looking at a queue of undecided cards: "everything needs to
// have a date displayed on the card, I can't tell if these are recent or just old
// and I should close." Age IS the question, so the relative form leads. The absolute
// timestamp goes on hover, for when the exact day is what matters.

/** Parse to a printable age, or null if the value isn't a usable date. Guarding here
 *  matters: relativeTime() on an unparseable string yields a cheerful "NaNd ago". */
function age(iso: string | null | undefined, now: Date): string | null {
  if (!iso) return null
  const t = new Date(iso)
  if (Number.isNaN(t.getTime())) return null
  return relativeTime(iso, now)
}

/** Absolute form for the tooltip — local time, unambiguous month. */
function absolute(iso: string): string {
  const t = new Date(iso)
  if (Number.isNaN(t.getTime())) return ''
  return t.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function ItemAge({
  createdAt,
  decidedAt,
  now = new Date(),
}: {
  createdAt: string
  decidedAt?: string | null
  /** Injected in tests; defaulted so neither card surface has to thread it through. */
  now?: Date
}): JSX.Element | null {
  const created = age(createdAt, now)
  if (!created) return null
  const decided = age(decidedAt, now)

  return (
    <span data-testid="item-age" title={absolute(createdAt)} className="whitespace-nowrap">
      {created}
      {decided && <span className="text-foreground-subtle"> · decided {decided}</span>}
    </span>
  )
}
