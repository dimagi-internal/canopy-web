/**
 * The host-side chrome: container, launcher, and the three display modes.
 *
 * An iframe cannot paint outside its own box, so the launcher bubble and the
 * panel frame are host DOM wrapping the iframe — the standard shape for this
 * class of widget. That host DOM lives in a **shadow root**, because the whole
 * promise is "drop this on any page": a host's `button { … }` or a global
 * `* { box-sizing: … }` must not be able to reach in and deform the launcher,
 * and our styles must not leak out onto the host either.
 *
 * The three modes differ only in how the container is positioned:
 *
 *   overlay — fixed bubble bottom-right, floating panel above it. Host layout
 *             untouched, so this is the mode that works on a page nobody
 *             modified.
 *   docked  — fixed full-height rail on the right. Proven in connect-labs,
 *             whose workflow runner already docks its own chat with `mr-96`.
 *   inline  — fills a host-provided element, no launcher and always open. The
 *             host owns placement and sizing.
 */

import {
  clampToViewport,
  isDrag,
  panelPosition,
  readSaved,
  storageKeyFor,
  writeSaved,
  type Point,
} from './dragging'
import { launcherVars, resolvedMode, type SafeTheme } from './theme'

export type DisplayMode = 'overlay' | 'inline' | 'docked'

export interface ChromeOptions {
  mode: DisplayMode
  /** Required for `inline`: a selector or element to fill. */
  target?: string | Element
  launcherLabel: string
  /** Whether the launcher carries an × that hides it. */
  dismissible: boolean
  /** Names the saved position, so two widgets on one origin do not fight. */
  app: string
  /** Where a dragged position is remembered.
   *
   *  Injected rather than reached for, because `window.localStorage` is not
   *  functional in this repo's jsdom setup — code that reads it directly is
   *  untestable here, which is how a persistence bug would ship unnoticed.
   *  `null` disables remembering without disabling dragging. */
  storage: Pick<Storage, 'getItem' | 'setItem'> | null
  title: string
  /** Panel width for overlay/docked, in px. */
  width: number
  zIndex: number
  /** Already validated — see `theme.normalizeTheme`. */
  theme: SafeTheme
  onToggle: (open: boolean) => void
}

export interface Chrome {
  iframe: HTMLIFrameElement
  open(): void
  close(): void
  toggle(): void
  isOpen(): boolean
  /** Hide the launcher for the rest of this page load. */
  dismiss(): void
  isDismissed(): boolean
  setHeight(px: number): void
  /** Re-dress the launcher and the panel frame. The panel's CONTENTS are
   *  canopy's document and are re-themed over the handshake, not here. */
  setTheme(theme: SafeTheme): void
  /** Show or hide the launcher for the current page, reversibly. */
  setLauncherVisible(visible: boolean): void
  destroy(): void
}

/**
 * Every colour, radius and font the host may change is a `--canopy-*` custom
 * property with TODAY's value as its fallback — so a host that sets nothing
 * gets exactly the widget it always got, and one that sets a variable (from
 * its own stylesheet: custom properties inherit through the shadow boundary,
 * and `all: initial` below deliberately does not reset them) gets it everywhere
 * that value is used.
 *
 * Hover darkens with `filter` rather than a second hard-coded shade, so it
 * works for whatever accent a host picks.
 *
 * `part=` on the launcher, the × and the panel frame is the escape hatch for a
 * host that wants more than the variables (size, shadow, letter-spacing):
 * `[data-canopy-widget]::part(launcher) { … }`. It reaches only what a host
 * names explicitly, so the isolation this root exists for is intact.
 */
const STYLES = `
  :host { all: initial; }
  * { box-sizing: border-box; }
  .panel {
    position: fixed; right: 16px; bottom: 84px;
    width: var(--w); height: min(640px, calc(100vh - 120px));
    border: 1px solid var(--canopy-panel-border, rgba(255,255,255,.12));
    border-radius: var(--canopy-radius, 12px);
    overflow: hidden; background: var(--canopy-panel-background, #1c1917);
    box-shadow: 0 12px 40px rgba(0,0,0,.45);
    display: none;
  }
  .panel[data-theme="light"] {
    border-color: var(--canopy-panel-border, rgba(0,0,0,.12));
    background: var(--canopy-panel-background, #fafaf9);
    box-shadow: 0 12px 40px rgba(0,0,0,.18);
  }
  .panel[data-open="true"] { display: block; }
  .panel[data-mode="docked"] {
    right: 0; top: 0; bottom: 0; width: var(--w);
    height: 100vh; border-radius: 0; border-width: 0 0 0 1px;
  }
  .panel[data-mode="inline"] {
    position: static; right: auto; bottom: auto;
    width: 100%; height: 100%; border-radius: 0; border-width: 0;
    box-shadow: none; display: block;
  }
  iframe { width: 100%; height: 100%; border: 0; display: block; }
  /* The launcher and its dismiss button travel together, so the X stays put
     relative to the bubble at any label length. Insets respect the phone's
     safe area — this sits over the home indicator otherwise, which is where a
     covered button hurts most. */
  .dock {
    position: fixed;
    right: calc(16px + env(safe-area-inset-right, 0px));
    bottom: calc(16px + env(safe-area-inset-bottom, 0px));
    display: flex; align-items: flex-start;
  }
  /* Once dragged the dock is placed by left/top, so the corner insets must stop
     applying or they fight the coordinates. */
  .dock[data-moved="true"] { right: auto; bottom: auto; }
  /* Suppressed by the HOST for a page where the bubble is wrong (it covers
     that page's own controls, or duplicates it). An attribute selector, not
     the hidden attribute: [hidden] loses to .dock { display: flex },
     which is the exact bug dismiss() records below. .dock[data-...] is more
     specific than .dock, so this one wins. */
  .dock[data-suppressed="true"] { display: none; }
  /* Without this a touch-drag scrolls the host's page instead of moving the
     bubble — the browser claims the gesture before pointermove ever fires. */
  .launcher { touch-action: none; }
  .launcher:active { cursor: grabbing; }
  .launcher {
    height: 48px; padding: 0 18px;
    /* A pill unless the host says otherwise. A host's general radius (from
       theme.radius or --canopy-radius) squares it too — a host that asked for
       8px corners and got a pill beside its own 6px buttons said so. The
       launcher-specific variable overrides both, for a pill on a squared panel. */
    border-radius: var(--canopy-launcher-radius, var(--canopy-radius, 24px));
    border: 0; cursor: pointer;
    background: var(--canopy-accent, #c2410c);
    color: var(--canopy-accent-foreground, #fff);
    font: 500 14px/1 var(--canopy-font, system-ui, sans-serif);
    box-shadow: 0 6px 20px rgba(0,0,0,.35);
    max-width: min(60vw, 260px);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    transition: filter .12s ease;
  }
  .launcher:hover { filter: brightness(.88); }
  .launcher:focus-visible {
    outline: 2px solid var(--canopy-focus-ring, #fff); outline-offset: 2px;
  }
  /* 28px, not the 44px a primary control would get: this is the ESCAPE hatch
     next to the thing you actually came for, and a dismiss the size of the
     button it sits on gets pressed by accident. It overlaps the bubble's
     corner, so the bubble keeps its own full target. */
  .dismiss {
    position: relative; left: -14px; top: -6px;
    width: 28px; height: 28px; border-radius: 14px;
    border: 0; cursor: pointer; padding: 0;
    background: #1c1917; color: #fafaf9;
    font: 500 15px/1 var(--canopy-font, system-ui, sans-serif);
    box-shadow: 0 2px 8px rgba(0,0,0,.4);
  }
  .dismiss:hover { filter: brightness(1.3); }
  :host([data-theme="light"]) .dismiss {
    background: #fff; color: #1c1917;
    box-shadow: 0 1px 2px rgba(0,0,0,.2), 0 2px 8px rgba(0,0,0,.18);
  }
  :host([data-theme="light"]) .dismiss:hover { filter: brightness(.94); }
  .dismiss:focus-visible {
    outline: 2px solid var(--canopy-focus-ring, #fff); outline-offset: 2px;
  }
`

function resolveTarget(target: string | Element | undefined): Element {
  if (!target) {
    throw new Error(
      "canopy: mode 'inline' needs a `target` element to fill. " +
        "Use mode 'overlay' or 'docked' to have the widget position itself.",
    )
  }
  const el = typeof target === 'string' ? document.querySelector(target) : target
  if (!el) {
    throw new Error(`canopy: no element matches target ${JSON.stringify(target)}`)
  }
  return el
}

export function createChrome(src: string, options: ChromeOptions): Chrome {
  const inline = options.mode === 'inline'
  const mount = inline ? resolveTarget(options.target) : document.body

  const host = document.createElement('div')
  host.setAttribute('data-canopy-widget', '')
  if (!inline) host.style.zIndex = String(options.zIndex)
  const root = host.attachShadow({ mode: 'open' })

  const style = document.createElement('style')
  style.textContent = STYLES
  root.appendChild(style)

  // The variables we were handed, on the HOST element: inline, so an explicit
  // `theme` option outranks the same names inherited from a host stylesheet,
  // and cleared on each call so a re-theme can remove what it no longer sets.
  let appliedVars: string[] = []
  function applyTheme(theme: SafeTheme): void {
    for (const name of appliedVars) host.style.removeProperty(name)
    const vars = launcherVars(theme)
    for (const [name, value] of Object.entries(vars)) host.style.setProperty(name, value)
    appliedVars = Object.keys(vars)
    const mode = resolvedMode(theme.mode)
    host.dataset.theme = mode
    panel.dataset.theme = mode
  }

  const panel = document.createElement('div')
  panel.className = 'panel'
  panel.dataset.mode = options.mode
  panel.style.setProperty('--w', `${options.width}px`)
  // Inline is always open: there is no launcher to open it with, and a host that
  // asked for it in their own layout has already decided it should be visible.
  panel.dataset.open = String(inline)
  panel.setAttribute('part', 'panel')
  applyTheme(options.theme)

  const iframe = document.createElement('iframe')
  iframe.src = src
  iframe.title = options.title
  // A cross-origin frame needs these explicitly, and nothing more: scripts to
  // run, same-origin so it can reach its own canopy APIs and storage, and forms
  // for the composer. Deliberately NOT allow-top-navigation — a widget must not
  // be able to navigate the page it is embedded in.
  iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-popups')
  panel.appendChild(iframe)
  root.appendChild(panel)

  let launcher: HTMLButtonElement | null = null
  let dock: HTMLDivElement | null = null
  //: Survives the dock being removed, which is what dismissing does.
  let dismissed = false
  if (!inline) {
    dock = document.createElement('div')
    dock.className = 'dock'

    launcher = document.createElement('button')
    launcher.type = 'button'
    launcher.className = 'launcher'
    launcher.setAttribute('part', 'launcher')
    launcher.textContent = options.launcherLabel
    launcher.title = options.launcherLabel
    launcher.setAttribute('aria-expanded', 'false')
    launcher.addEventListener('click', () => api.toggle())
    dock.appendChild(launcher)

    if (options.dismissible) {
      const dismiss = document.createElement('button')
      dismiss.type = 'button'
      dismiss.className = 'dismiss'
      dismiss.setAttribute('part', 'dismiss')
      dismiss.textContent = '×'
      // The bubble sits over the host's own page, and on a phone it lands on
      // top of whatever is in the bottom-right corner. Somebody who wants the
      // page rather than the agent needs a way to say so that is not "reload
      // and hope".
      dismiss.setAttribute('aria-label', `Hide ${options.launcherLabel} on this page`)
      dismiss.title = 'Hide on this page'
      // No stopPropagation: the X is a SIBLING of the launcher inside the dock,
      // overlapping it only visually, so a click on it never passes through the
      // bubble. A guard here would read as though it did.
      dismiss.addEventListener('click', () => api.dismiss())
      dock.appendChild(dismiss)
    }

    // --- dragging -----------------------------------------------------------
    //
    // The bubble is fixed to a corner of somebody else's page, and on a phone
    // that corner already has something in it. Dismissing hides it; dragging
    // moves it, and the position is remembered — unlike dismissal, a moved
    // bubble is still visible, so there is no way to strand yourself.
    const storageKey = storageKeyFor(options.app)
    const storage = options.storage

    const place = (pos: Point) => {
      const size = { width: dock!.offsetWidth || 140, height: dock!.offsetHeight || 48 }
      const at = clampToViewport(pos, size, {
        width: window.innerWidth,
        height: window.innerHeight,
      })
      dock!.dataset.moved = 'true'
      dock!.style.left = `${at.x}px`
      dock!.style.top = `${at.y}px`
      return at
    }

    const saved = readSaved(storage, storageKey)
    if (saved) place(saved)

    let origin: Point | null = null
    let grabOffset: Point = { x: 0, y: 0 }
    let moved = false

    launcher.addEventListener('pointerdown', (event) => {
      origin = { x: event.clientX, y: event.clientY }
      moved = false
      const rect = dock!.getBoundingClientRect()
      grabOffset = { x: event.clientX - rect.left, y: event.clientY - rect.top }
      // Keeps the events coming even when the pointer leaves the bubble, which
      // it does immediately on any real drag.
      launcher!.setPointerCapture?.(event.pointerId)
    })

    launcher.addEventListener('pointermove', (event) => {
      if (!origin) return
      const now = { x: event.clientX, y: event.clientY }
      if (!moved && !isDrag(origin, now)) return
      moved = true
      place({ x: now.x - grabOffset.x, y: now.y - grabOffset.y })
    })

    const endDrag = (event: PointerEvent) => {
      if (!origin) return
      launcher!.releasePointerCapture?.(event.pointerId)
      origin = null
      if (!moved) return
      const rect = dock!.getBoundingClientRect()
      writeSaved(storage, storageKey, { x: rect.left, y: rect.top })
      if (api.isOpen()) placePanel()
    }
    launcher.addEventListener('pointerup', endDrag)
    launcher.addEventListener('pointercancel', endDrag)

    // A drag ends over the bubble often enough to matter, and the browser fires
    // a click when it does. Without this the panel opens every time you put the
    // bubble down where you picked it up.
    launcher.addEventListener('click', (event) => {
      if (moved) {
        event.stopImmediatePropagation()
        event.preventDefault()
        moved = false
      }
    }, true)

    // A window that changes size can strand a bubble that was fine before.
    window.addEventListener('resize', () => {
      if (dock?.dataset.moved !== 'true') return
      const rect = dock.getBoundingClientRect()
      place({ x: rect.left, y: rect.top })
      if (api.isOpen()) placePanel()
    })

    root.appendChild(dock)
  }

  mount.appendChild(host)

  /** Anchor the panel to wherever the bubble ended up.
   *
   *  Only once the dock has actually been moved — an untouched widget keeps the
   *  corner placement its CSS already gives it, so nothing changes for a host
   *  that never drags anything. */
  function placePanel() {
    if (inline || !dock || dock.dataset.moved !== 'true') return
    const d = dock.getBoundingClientRect()
    const at = panelPosition(
      { x: d.left, y: d.top, width: d.width, height: d.height },
      { width: panel.offsetWidth || options.width, height: panel.offsetHeight || 500 },
      { width: window.innerWidth, height: window.innerHeight },
    )
    panel.style.left = `${at.x}px`
    panel.style.top = `${at.y}px`
    panel.style.right = 'auto'
    panel.style.bottom = 'auto'
  }

  const api: Chrome = {
    iframe,
    isOpen: () => panel.dataset.open === 'true',
    open() {
      if (api.isOpen()) return
      panel.dataset.open = 'true'
      placePanel()
      launcher?.setAttribute('aria-expanded', 'true')
      options.onToggle(true)
    },
    close() {
      // Inline has no closed state — the host removed the launcher by choosing
      // this mode, so closing would leave no way back.
      if (inline || !api.isOpen()) return
      panel.dataset.open = 'false'
      launcher?.setAttribute('aria-expanded', 'false')
      options.onToggle(false)
    },
    toggle() {
      api.isOpen() ? api.close() : api.open()
    },
    dismiss() {
      // Closes first, so dismissing while open does not leave a panel on
      // screen with nothing to close it with.
      api.close()
      // REMOVED, not hidden. `dock.hidden = true` sets an attribute whose only
      // effect is the UA stylesheet's `[hidden] { display: none }` — and
      // `.dock { display: flex }` is a class selector, so it wins the cascade
      // and the bubble stayed on screen. Nothing in CSS can un-remove a node.
      //
      // jsdom does not reproduce that cascade (it reported `display: none` for
      // the broken version), so no computed-style test could have caught it —
      // which is why the test below asserts the node is GONE rather than
      // asserting anything about how it was hidden.
      dock?.remove()
      dismissed = true
    },
    isDismissed: () => dismissed,
    setHeight(px: number) {
      if (inline) return // the host owns layout here
      panel.style.height = `${px}px`
    },
    setTheme: applyTheme,
    setLauncherVisible(visible: boolean) {
      if (!dock) return // inline has no launcher
      // Hiding the launcher under an open panel would leave the panel with the
      // page's own controls beneath it and no bubble to fold it back into.
      // Closing is not ending: the conversation is still there when it reopens.
      if (!visible) api.close()
      dock.dataset.suppressed = String(!visible)
    },
    destroy() {
      host.remove()
    },
  }

  return api
}
