import { useLayoutEffect, useState, type CSSProperties, type RefObject } from 'react'

/** Gap kept between a popover and the viewport's edges, in px. */
const GUTTER = 8

/**
 * Where a popover of `width` hangs below `anchor` so that it stays inside a viewport
 * `viewport` px wide: right-aligned to the anchor when that fits, slid right until
 * its left edge is inside, and never wider than the viewport less its gutters.
 * Pure, so the arithmetic is tested without a layout engine.
 */
export function placeBelow(anchor: { left: number; right: number; bottom: number }, width: number,
                           viewport: number): { top: number; left: number; width: number } {
  const w = Math.max(0, Math.min(width, viewport - 2 * GUTTER))
  const left = Math.min(Math.max(GUTTER, anchor.right - w), Math.max(GUTTER, viewport - GUTTER - w))
  return { top: anchor.bottom + 4, left, width: w }
}

/**
 * A header popover that never opens off the edge of a narrow surface (the embedded
 * panel): fixed-positioned from its anchor's box, clamped into the viewport, and
 * re-placed when the window resizes or scrolls. `null` until it is open and placed.
 */
export function useAnchoredPopover(anchor: RefObject<HTMLElement | null>, open: boolean,
                                   width = 288): CSSProperties | undefined {
  const [style, setStyle] = useState<CSSProperties | undefined>(undefined)
  useLayoutEffect(() => {
    if (!open) {
      setStyle(undefined)
      return
    }
    const place = () => {
      const el = anchor.current
      if (!el) return
      const at = placeBelow(el.getBoundingClientRect(), width, window.innerWidth)
      setStyle({ position: 'fixed', top: at.top, left: at.left, width: at.width })
    }
    place()
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [anchor, open, width])
  return style
}
