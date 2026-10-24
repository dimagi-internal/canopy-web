import { Component, useEffect, useState, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'

import './frame.css'
import { CHANNEL, isMessage, type FrameMessage, type HostMessage, type ReadRequest, type ViewScope } from '../protocol'
import { compile, type ViewComponent } from './compile'
import { NavigateContext } from './kit'

/**
 * Entry point of the frame a dynamic view runs in (`/dynamic-views/frame/`,
 * apps/dynamic_views/frame.py). Not the app's entry: no router, no auth, no
 * service worker, no API client — the frame has no network, and asks the canopy
 * page for everything (`../protocol.ts`).
 *
 * `window.CANOPY_DV.origins` is injected by the server: the only origins whose
 * messages this frame accepts. The page that framed it is one of them; the view's
 * own code runs in this same window and so could post messages too, but only to
 * the parent, and only the ones the page already answers for it.
 */

declare global {
  interface Window {
    CANOPY_DV?: { origins: string[] }
  }
}

const ORIGINS = window.CANOPY_DV?.origins ?? []
// Where the browser says who framed us (Chrome, Safari), start talking only to
// that origin — if it is one the server named — instead of offering each in turn.
const ancestor = (location as Location & { ancestorOrigins?: DOMStringList }).ancestorOrigins?.[0]
let parentOrigin: string | null = ancestor && ORIGINS.includes(ancestor) ? ancestor : null

function toHost(msg: FrameMessage) {
  // Until the page has spoken we do not know which canopy origin framed us; a
  // message to a non-matching targetOrigin is dropped, so this delivers once.
  for (const origin of parentOrigin ? [parentOrigin] : ORIGINS) window.parent.postMessage(msg, origin)
}

let nextId = 1
const pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void }>()

const canopy = {
  read(model: string, opts: Omit<ReadRequest, 'model'> = {}): Promise<unknown> {
    const id = nextId++
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject })
      toHost({ channel: CHANNEL, type: 'read', id, request: { model, params: opts.params, path: opts.path } })
    })
  },
  refresh() {
    toHost({ channel: CHANNEL, type: 'refresh' })
  },
  navigate(path: string) {
    if (typeof path === 'string' && path) toHost({ channel: CHANNEL, type: 'navigate', path })
  },
}

class Boundary extends Component<{ children: ReactNode; resetKey: unknown }, { error: Error | null }> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) {
    return { error }
  }
  componentDidCatch(error: Error) {
    toHost({ channel: CHANNEL, type: 'error', message: error.message })
  }
  componentDidUpdate(prev: { resetKey: unknown }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null })
  }
  render() {
    if (this.state.error) return <Failure title="This view crashed" message={this.state.error.message} />
    return this.props.children
  }
}

function Failure({ title, message }: { title: string; message: string }) {
  return (
    <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm">
      <div className="font-semibold text-destructive">{title}</div>
      <pre className="mt-2 whitespace-pre-wrap font-mono text-xs text-foreground">{message}</pre>
    </div>
  )
}

type State =
  | { kind: 'waiting' }
  | { kind: 'failed'; message: string }
  | { kind: 'ready'; View: ViewComponent; data: Record<string, unknown>; scope: ViewScope; code: string }

function App() {
  const [state, setState] = useState<State>({ kind: 'waiting' })

  useEffect(() => {
    function onMessage(ev: MessageEvent) {
      if (ev.source !== window.parent || !ORIGINS.includes(ev.origin)) return
      if (parentOrigin && ev.origin !== parentOrigin) return
      parentOrigin = ev.origin
      const msg = ev.data as HostMessage
      if (!isMessage(msg)) return
      if (msg.type === 'render') {
        document.documentElement.classList.toggle('dark', msg.theme === 'dark')
        try {
          const View = compile(msg.code)
          setState({ kind: 'ready', View, data: msg.data ?? {}, scope: msg.scope, code: msg.code })
        } catch (e) {
          const message = e instanceof Error ? e.message : String(e)
          toHost({ channel: CHANNEL, type: 'error', message })
          setState({ kind: 'failed', message })
        }
      } else if (msg.type === 'data') {
        setState((s) => (s.kind === 'ready' ? { ...s, data: msg.data ?? {} } : s))
      } else if (msg.type === 'read-result') {
        const waiter = pending.get(msg.id)
        if (!waiter) return
        pending.delete(msg.id)
        if (msg.ok) waiter.resolve(msg.data)
        else waiter.reject(new Error(msg.error))
      }
    }
    window.addEventListener('message', onMessage)
    toHost({ channel: CHANNEL, type: 'ready' })
    return () => window.removeEventListener('message', onMessage)
  }, [])

  if (state.kind === 'waiting') return null
  if (state.kind === 'failed') return <Failure title="This view could not be compiled" message={state.message} />
  const { View, data, scope, code } = state
  return (
    <NavigateContext.Provider value={canopy.navigate}>
      <Boundary resetKey={code}>
        <View data={data} scope={scope} canopy={canopy} />
      </Boundary>
    </NavigateContext.Provider>
  )
}

// A plain <a href> in the view leaves through the page, like `Link` does: the
// frame cannot (and must not) navigate canopy itself.
document.addEventListener('click', (ev) => {
  const a = (ev.target as Element | null)?.closest?.('a[href]') as HTMLAnchorElement | null
  if (!a || ev.defaultPrevented) return
  const href = a.getAttribute('href') || ''
  if (href.startsWith('blob:') || href.startsWith('data:') || href.startsWith('#') || a.hasAttribute('download')) return
  ev.preventDefault()
  canopy.navigate(href)
})

const root = document.getElementById('dv-root')
if (window.self === window.top || !root) {
  document.body.textContent = 'This page only runs inside canopy.'
} else {
  // Tell the page our height whenever it changes. Two triggers, because neither
  // alone is enough: Chrome does not run rendering — and so never delivers a
  // ResizeObserver callback — in a cross-origin frame that is off-screen (a view
  // below the fold on a project page stayed clipped at its first height), while
  // a MutationObserver is a microtask and always runs. The ResizeObserver still
  // catches growth with no DOM change (an image or a font arriving).
  let last = -1
  const report = () => {
    const height = Math.ceil(root.getBoundingClientRect().height)
    if (height === last) return
    last = height
    toHost({ channel: CHANNEL, type: 'resize', height })
  }
  new ResizeObserver(report).observe(root)
  new MutationObserver(report).observe(root, { childList: true, subtree: true, characterData: true, attributes: true })
  createRoot(root).render(<App />)
}
