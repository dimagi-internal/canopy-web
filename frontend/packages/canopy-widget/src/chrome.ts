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

export type DisplayMode = 'overlay' | 'inline' | 'docked'

export interface ChromeOptions {
  mode: DisplayMode
  /** Required for `inline`: a selector or element to fill. */
  target?: string | Element
  launcherLabel: string
  title: string
  /** Panel width for overlay/docked, in px. */
  width: number
  zIndex: number
  onToggle: (open: boolean) => void
}

export interface Chrome {
  iframe: HTMLIFrameElement
  open(): void
  close(): void
  toggle(): void
  isOpen(): boolean
  setHeight(px: number): void
  destroy(): void
}

const STYLES = `
  :host { all: initial; }
  * { box-sizing: border-box; }
  .panel {
    position: fixed; right: 16px; bottom: 84px;
    width: var(--w); height: min(640px, calc(100vh - 120px));
    border: 1px solid rgba(255,255,255,.12); border-radius: 12px;
    overflow: hidden; background: #1c1917;
    box-shadow: 0 12px 40px rgba(0,0,0,.45);
    display: none;
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
  .launcher {
    position: fixed; right: 16px; bottom: 16px;
    height: 48px; padding: 0 18px; border-radius: 24px;
    border: 0; cursor: pointer;
    background: #c2410c; color: #fff;
    font: 500 14px/1 system-ui, sans-serif;
    box-shadow: 0 6px 20px rgba(0,0,0,.35);
  }
  .launcher:hover { background: #9a3412; }
  .launcher:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
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

  const panel = document.createElement('div')
  panel.className = 'panel'
  panel.dataset.mode = options.mode
  panel.style.setProperty('--w', `${options.width}px`)
  // Inline is always open: there is no launcher to open it with, and a host that
  // asked for it in their own layout has already decided it should be visible.
  panel.dataset.open = String(inline)

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
  if (!inline) {
    launcher = document.createElement('button')
    launcher.type = 'button'
    launcher.className = 'launcher'
    launcher.textContent = options.launcherLabel
    launcher.setAttribute('aria-expanded', 'false')
    launcher.addEventListener('click', () => api.toggle())
    root.appendChild(launcher)
  }

  mount.appendChild(host)

  const api: Chrome = {
    iframe,
    isOpen: () => panel.dataset.open === 'true',
    open() {
      if (api.isOpen()) return
      panel.dataset.open = 'true'
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
    setHeight(px: number) {
      if (inline) return // the host owns layout here
      panel.style.height = `${px}px`
    },
    destroy() {
      host.remove()
    },
  }

  return api
}
