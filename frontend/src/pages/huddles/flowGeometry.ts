/**
 * Where the conversation's lines run — pure geometry, so it is unit-testable
 * without a browser. Rects are relative to the grid.
 *
 * Two kinds of line:
 *  - the co-sign arc, from a partner's ANSWER ROW (round 3) to the PROPOSAL
 *    ROW it answers (round 2). It leaves and enters each card at its side
 *    edge, level with the row, so the end points name the exact line; between
 *    them it runs beneath the (opaque) cards, showing only in the gutters.
 *  - the exchange: the leader's round card → each member's card (the ask), and
 *    each member's card → the leader (the reply). These run in the horizontal
 *    gutter between rows — a bus above the row for asks, below it for replies —
 *    and drop into the cards' top / bottom edges, so they never cross text.
 */

export type Rect = { x: number; y: number; w: number; h: number }
export type Pt = { x: number; y: number }

const r1 = (n: number) => Math.round(n * 10) / 10

/** The point on `card`'s side edge level with the middle of `row`. */
export function rowAnchor(row: Rect, card: Rect, side: 'left' | 'right'): Pt {
  return { x: side === 'left' ? card.x : card.x + card.w, y: row.y + row.h / 2 }
}

/** A co-sign arc from an answer row to a proposal row. */
export function flowArc(fromRow: Rect, fromCard: Rect, toRow: Rect, toCard: Rect): { d: string; start: Pt; end: Pt } {
  const fc = fromCard.x + fromCard.w / 2
  const tc = toCard.x + toCard.w / 2
  let start: Pt
  let end: Pt
  let c1: number
  let c2: number
  if (Math.abs(fc - tc) < 4) {
    // Same column: out of the left edges and back, bowed into the gutter.
    start = rowAnchor(fromRow, fromCard, 'left')
    end = rowAnchor(toRow, toCard, 'left')
    c1 = start.x - 40
    c2 = end.x - 40
  } else if (fc > tc) {
    start = rowAnchor(fromRow, fromCard, 'left')
    end = rowAnchor(toRow, toCard, 'right')
    const k = Math.max(24, (start.x - end.x) * 0.5)
    c1 = start.x - k
    c2 = end.x + k
  } else {
    start = rowAnchor(fromRow, fromCard, 'right')
    end = rowAnchor(toRow, toCard, 'left')
    const k = Math.max(24, (end.x - start.x) * 0.5)
    c1 = start.x + k
    c2 = end.x - k
  }
  return {
    d: `M ${r1(start.x)} ${r1(start.y)} C ${r1(c1)} ${r1(start.y)} ${r1(c2)} ${r1(end.y)} ${r1(end.x)} ${r1(end.y)}`,
    start, end,
  }
}

/** The ask: out of the leader card's right edge, up to the bus in the gutter
 * above the row, along it, and down into the member card's top. */
export function askPath(leader: Rect, member: Rect, busY: number): string {
  const sx = leader.x + leader.w
  const sy = leader.y + 14
  const gx = sx + 6
  const dx = member.x + 22
  return `M ${r1(sx)} ${r1(sy)} L ${r1(gx)} ${r1(sy)} L ${r1(gx)} ${r1(busY)} L ${r1(dx)} ${r1(busY)} L ${r1(dx)} ${r1(member.y)}`
}

/** The reply: out of the member card's bottom, down to the bus in the gutter
 * below the row, back along it, and up into the leader card's bottom. */
export function replyPath(member: Rect, leader: Rect, busY: number): string {
  const sx = member.x + member.w - 22
  const sy = member.y + member.h
  const ex = leader.x + leader.w - 22
  return `M ${r1(sx)} ${r1(sy)} L ${r1(sx)} ${r1(busY)} L ${r1(ex)} ${r1(busY)} L ${r1(ex)} ${r1(leader.y + leader.h)}`
}
