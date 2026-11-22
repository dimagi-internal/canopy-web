/**
 * A person's recent gesture on one frame, for what a click unlocks but a drag or a dialog outlives.
 *
 * A write to a connection, a download, a copy or a new tab needs a click on the frame, which the page
 * reads from its OWN `navigator.userActivation` (`pageClickedFrame`). That activation lasts a few
 * seconds and only events like pointerdown and keydown start it: a kanban card's `drop` is not one,
 * and a long drag outlives it. So the frame reports each pointerdown and keydown (`gesture`, a message
 * with no claim in it), and the page BELIEVES one only when it sees a live click on that same frame as
 * the message arrives. A believed gesture is remembered for `GESTURE_WINDOW_MS`; a write that finds
 * neither a live lease nor a live click may open a lease on it, once (`spend`).
 *
 * It grants nothing a click did not: one click could already open a lease (`canopy.connection.begin`)
 * good for ten minutes and 500 writes. The server's lease, budget and audit are unchanged.
 * Free of the DOM (the click test is passed in): gestures.test.ts.
 */

/** How long after a real pointerdown/keydown on the frame a write may still open a lease. */
export const GESTURE_WINDOW_MS = 60_000

export class Gestures {
  private at = -Infinity
  private clicked: () => boolean
  private now: () => number

  constructor(clicked: () => boolean, now: () => number = Date.now) {
    this.clicked = clicked
    this.now = now
  }

  /** The frame said a gesture happened: remember it only if the page sees a live click on the frame now. */
  note(): void {
    if (this.clicked()) this.at = this.now()
  }

  /** A remembered gesture within the window. `spend` uses it up, so one gesture opens one lease. */
  recent(spend = false): boolean {
    const ok = this.now() - this.at < GESTURE_WINDOW_MS
    if (ok && spend) this.at = -Infinity
    return ok
  }

  /** A live click now, or a remembered gesture (not spent): what a download needs. */
  active(): boolean {
    if (this.clicked()) {
      this.at = this.now()
      return true
    }
    return this.recent()
  }

  forget(): void {
    this.at = -Infinity
  }
}
