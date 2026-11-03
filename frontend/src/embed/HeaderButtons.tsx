import { Maximize2, Minimize2, Pin } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { HostLink } from './hostLink'

/** Whether the host has the panel expanded, as it last said — the host owns
 *  the panel's box, so the frame follows its answer rather than its own guess. */
export function useExpanded(link: HostLink): boolean {
  const [expanded, setExpanded] = useState(() => link.isExpanded())
  useEffect(() => link.onDisplayChanged(setExpanded), [link])
  return expanded
}

/** One size and hover for every icon control in the panel header, so the pin,
 *  expand and close read as one row rather than three glyphs of different
 *  widths. `aria-pressed` paints the on state of a toggle. */
export const HEADER_ICON =
  'inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors ' +
  'hover:bg-muted hover:text-foreground aria-pressed:bg-muted aria-pressed:text-foreground'

/** Expand / collapse, beside ×. Drawn only when the host allows it (`init`). */
export function ExpandButton({ link, expandable }: { link: HostLink; expandable: boolean }) {
  const expanded = useExpanded(link)
  if (!expandable) return null
  const label = expanded ? 'Collapse panel' : 'Expand panel'
  return (
    <button
      type="button"
      onClick={() => link.requestExpand(!expanded)}
      aria-label={label}
      aria-pressed={expanded}
      title={expanded ? 'Collapse (Esc)' : 'Expand'}
      className={HEADER_ICON}
    >
      {expanded ? (
        <Minimize2 className="h-3.5 w-3.5" aria-hidden />
      ) : (
        <Maximize2 className="h-3.5 w-3.5" aria-hidden />
      )}
    </button>
  )
}

function useSnapped(link: HostLink): boolean {
  const [snapped, setSnapped] = useState(() => link.isSnapped())
  useEffect(() => link.onSnapChanged(setSnapped), [link])
  return snapped
}

/** Snap a docked rail into the page (the page makes room) or float it over the
 *  page again. Drawn only when the host allows it, and not while expanded —
 *  an expanded panel covers the page whichever way it is set. */
export function SnapButton({ link, snappable }: { link: HostLink; snappable: boolean }) {
  const snapped = useSnapped(link)
  const expanded = useExpanded(link)
  if (!snappable || expanded) return null
  const label = snapped ? 'Float over the page' : 'Snap into the page'
  return (
    <button
      type="button"
      onClick={() => link.requestSnap(!snapped)}
      aria-label={label}
      aria-pressed={snapped}
      title={label}
      className={HEADER_ICON}
    >
      {/* A pin that is pressed in, not a struck-through one: the crossed-out
          glyph read as "broken" sitting in the header of a pinned panel. */}
      <Pin className={snapped ? 'h-3.5 w-3.5 fill-current' : 'h-3.5 w-3.5'} aria-hidden />
    </button>
  )
}
