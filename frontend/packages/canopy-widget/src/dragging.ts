/**
 * The geometry behind dragging the launcher, kept away from the DOM.
 *
 * Pure functions, so the rules that actually matter — a bubble never lands
 * off-screen, a persisted position survives a window that has since been made
 * smaller, a tap is not mistaken for a drag — are testable without a browser.
 * The DOM half in `chrome.ts` stays thin enough to read.
 *
 * This split is not decoration. jsdom does not do layout, so anything phrased
 * as "is it in the right place" can only be checked here; a test written
 * against the elements would assert that a style string was set and tell you
 * nothing about whether the thing is reachable, which is the mistake the
 * dismiss button already shipped once.
 */

export interface Point {
  x: number
  y: number
}

export interface Viewport {
  width: number
  height: number
}

/** How far a pointer must travel before it is a drag and not a tap. */
export const DRAG_THRESHOLD_PX = 5

/** Keep at least this much of the bubble on screen, on every side. */
export const EDGE_MARGIN_PX = 8

/**
 * Pin a position inside the viewport.
 *
 * Applied on every move AND on restore, because the window the position was
 * saved in is not the window it is read back into: rotate a phone, or open the
 * laptop lid on a smaller external display, and a faithfully restored position
 * is off-screen with no way to drag it back.
 */
export function clampToViewport(
  pos: Point,
  size: { width: number; height: number },
  viewport: Viewport,
  margin: number = EDGE_MARGIN_PX,
): Point {
  // `Math.max(margin, …)` second, so that on a viewport too small to hold the
  // element the bubble sits at the top-left edge rather than being pushed off
  // the other side by a negative upper bound.
  const maxX = viewport.width - size.width - margin
  const maxY = viewport.height - size.height - margin
  return {
    x: Math.max(margin, Math.min(pos.x, Math.max(margin, maxX))),
    y: Math.max(margin, Math.min(pos.y, Math.max(margin, maxY))),
  }
}

/** Has the pointer moved far enough to mean it? */
export function isDrag(from: Point, to: Point, threshold: number = DRAG_THRESHOLD_PX): boolean {
  return Math.hypot(to.x - from.x, to.y - from.y) >= threshold
}

/**
 * Where the panel goes, given where the bubble ended up.
 *
 * Above the bubble when there is room, below it when there is not, and pinned
 * inside the viewport either way — so dragging the launcher to the top of the
 * screen does not open a panel that runs off the bottom.
 */
export function panelPosition(
  dock: { x: number; y: number; width: number; height: number },
  panel: { width: number; height: number },
  viewport: Viewport,
  gap = 12,
): Point {
  const above = dock.y - panel.height - gap
  const below = dock.y + dock.height + gap
  const y = above >= EDGE_MARGIN_PX ? above : below
  // Right-aligned with the bubble, which is where it sat before anything moved.
  const x = dock.x + dock.width - panel.width
  return clampToViewport({ x, y }, panel, viewport)
}

/** Read a saved position. Never throws, and never returns something unusable. */
export function readSaved(storage: Pick<Storage, 'getItem'> | null, key: string): Point | null {
  if (!storage) return null
  try {
    const raw = storage.getItem(key)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<Point>
    if (typeof parsed?.x !== 'number' || typeof parsed?.y !== 'number') return null
    if (!Number.isFinite(parsed.x) || !Number.isFinite(parsed.y)) return null
    return { x: parsed.x, y: parsed.y }
  } catch {
    // Private windows, blocked site data, and a value some other version of
    // this code wrote. A lost position is a bubble in its default corner,
    // which is exactly where it starts anyway.
    return null
  }
}

export function writeSaved(
  storage: Pick<Storage, 'setItem'> | null,
  key: string,
  pos: Point,
): void {
  try {
    storage?.setItem(key, JSON.stringify(pos))
  } catch {
    // Quota, private mode, a host that disabled storage. Losing the memory of
    // where somebody put it must never break moving it.
  }
}

/** Per app, so two widgets on one origin do not fight over one position. */
export function storageKeyFor(app: string): string {
  return `canopy.widget.position.${app}`
}
