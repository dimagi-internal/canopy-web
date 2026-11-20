/**
 * canopy's side of the Dimagi Canopy extension's panel: wire protocol v1 between the
 * extension's `panel.html` (our parent, `chrome-extension://<id>`) and `/ext/panel/`
 * (this page), plus the gate every sandboxed app page's `canopy.browser` call passes.
 * Design: docs/architecture/browser-extension.md.
 *
 * Two hops, both checked here, by the canopy page — never by the sandboxed app code:
 *
 * - **extension ⇄ panel** (`ExtensionLink`): we post only to an ALLOWED extension
 *   origin that is actually our parent (`location.ancestorOrigins`, else the origin of
 *   the first well-formed message from `window.parent`), and accept a message only when
 *   `event.source === window.parent` and `event.origin` is that origin. Every request
 *   carries a fresh id and fails `timeout` after `REQUEST_TIMEOUT_MS`.
 * - **app page ⇄ panel** (`BrowserGate`): a call is relayed only when the capability (or
 *   event) is in the page's `browser` declaration AND the current tab's URL matches the
 *   page's patterns; the page sees the tab only while it matches.
 */
import { matchesAny } from './matchPattern'

export const PREFIX = 'canopy-ext:'
export const VERSION = 1
export const REQUEST_TIMEOUT_MS = 10_000

export type ErrorCode = 'no_adapter' | 'not_ready' | 'unknown_capability' | 'adapter_error' | 'timeout' | 'bad_request'

export interface Tab { id: number; url: string; title: string }
export interface AdapterInfo { name: string; version?: string; capabilities?: string[]; events?: string[] }
export interface Hello { extensionVersion: string; adapters: AdapterInfo[] }
export interface Context { tab: Tab | null; adapters: { name: string; ready: boolean }[] }
export interface AdapterEvent { tabId: number; adapter: string; event: string; data: unknown }

/** What a `canopy.browser` call rejects with: a sentence, and the protocol's code. */
export class BrowserError extends Error {
  code: ErrorCode
  constructor(code: ErrorCode, message: string) {
    super(message)
    this.code = code
  }
}

/** The allowed extension origins the server put in the shell (`apps/embedding/extension.py`). */
export function allowedOrigins(doc: Pick<Document, 'querySelector'> = document): string[] {
  const raw = doc.querySelector('meta[name="canopy-ext-origins"]')?.getAttribute('content')
  try {
    const list = raw ? JSON.parse(raw) : []
    return Array.isArray(list) ? list.filter((o): o is string => typeof o === 'string' && o.startsWith('chrome-extension://')) : []
  } catch {
    return []
  }
}

type Post = (msg: Record<string, unknown>, targetOrigin: string) => void

export interface LinkEnv {
  /** Our parent's window (the extension's panel page). */
  parent: unknown
  post: Post
  /** `location.ancestorOrigins[0]`, where the browser says who framed us. */
  ancestor?: string | null
  setTimeout?: (fn: () => void, ms: number) => unknown
  clearTimeout?: (t: unknown) => void
  newId?: () => string
}

interface Handlers {
  hello?: (h: Hello) => void
  context?: (c: Context) => void
  event?: (e: AdapterEvent) => void
}

function isObj(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v)
}

function parseTab(v: unknown): Tab | null {
  if (!isObj(v) || typeof v.id !== 'number' || typeof v.url !== 'string') return null
  return { id: v.id, url: v.url, title: typeof v.title === 'string' ? v.title : '' }
}

let counter = 0
function defaultId(): string {
  counter += 1
  const rnd = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : Math.random().toString(36).slice(2)
  return `${counter}-${rnd}`
}

/** The panel's link to the extension that frames it. */
export class ExtensionLink {
  private origin: string | null
  private pending = new Map<string, { resolve: (v: unknown) => void; reject: (e: Error) => void; timer: unknown }>()
  private env: Required<Omit<LinkEnv, 'ancestor'>>
  hello: Hello | null = null
  context: Context | null = null

  private allowed: readonly string[]
  private handlers: Handlers

  constructor(allowed: readonly string[], env: LinkEnv, handlers: Handlers = {}) {
    this.allowed = allowed
    this.handlers = handlers
    this.env = {
      parent: env.parent,
      post: env.post,
      setTimeout: env.setTimeout ?? ((fn, ms) => setTimeout(fn, ms)),
      clearTimeout: env.clearTimeout ?? ((t) => clearTimeout(t as ReturnType<typeof setTimeout>)),
      newId: env.newId ?? defaultId,
    }
    this.origin = env.ancestor && allowed.includes(env.ancestor) ? env.ancestor : null
  }

  /** The extension origin we talk to, once known. */
  get extensionOrigin(): string | null {
    return this.origin
  }

  private send(msg: Record<string, unknown>) {
    const full = { ...msg, v: VERSION }
    // Until the extension has spoken (or the browser named it), offer each allowed origin:
    // a post to an origin that is not the parent's is dropped by the browser.
    for (const o of this.origin ? [this.origin] : this.allowed) this.env.post(full, o)
  }

  /** `canopy-ext:ready`: the extension answers with `hello` and `context`. */
  ready() {
    this.send({ type: `${PREFIX}ready` })
  }

  /** `canopy-ext:open`: the extension opens an https URL in a new tab. */
  open(url: string): void {
    let u: URL
    try {
      u = new URL(url)
    } catch {
      throw new BrowserError('bad_request', 'open() takes an https URL')
    }
    if (u.protocol !== 'https:') throw new BrowserError('bad_request', 'open() takes an https URL')
    this.send({ type: `${PREFIX}open`, url: u.toString() })
  }

  /** `canopy-ext:request`, answered by the `response` with the same id, or `timeout`. */
  request(tabId: number, adapter: string, capability: string, args: Record<string, unknown>): Promise<unknown> {
    if (!this.origin) return Promise.reject(new BrowserError('not_ready', 'The extension has not connected yet.'))
    const id = this.env.newId()
    return new Promise((resolve, reject) => {
      const timer = this.env.setTimeout(() => {
        this.pending.delete(id)
        reject(new BrowserError('timeout', `The ${adapter} tab did not answer within ${REQUEST_TIMEOUT_MS / 1000} seconds.`))
      }, REQUEST_TIMEOUT_MS)
      this.pending.set(id, { resolve, reject, timer })
      this.send({ type: `${PREFIX}request`, id, tabId, adapter, capability, args })
    })
  }

  /** Feed every `message` event here; returns whether it was the extension's. */
  receive(ev: { source: unknown; origin: string; data: unknown }): boolean {
    if (ev.source !== this.env.parent || !this.allowed.includes(ev.origin)) return false
    if (this.origin && ev.origin !== this.origin) return false
    const d = ev.data
    if (!isObj(d) || typeof d.type !== 'string' || !d.type.startsWith(PREFIX) || d.v !== VERSION) return false
    this.origin = ev.origin
    switch (d.type.slice(PREFIX.length)) {
      case 'hello': {
        const adapters = Array.isArray(d.adapters) ? d.adapters.filter(isObj).map((a) => ({
          name: String(a.name ?? ''), version: typeof a.version === 'string' ? a.version : undefined,
          capabilities: Array.isArray(a.capabilities) ? a.capabilities.map(String) : [],
          events: Array.isArray(a.events) ? a.events.map(String) : [],
        })) : []
        this.hello = { extensionVersion: String(d.extensionVersion ?? ''), adapters }
        this.handlers.hello?.(this.hello)
        return true
      }
      case 'context': {
        const adapters = Array.isArray(d.adapters)
          ? d.adapters.filter(isObj).map((a) => ({ name: String(a.name ?? ''), ready: a.ready === true }))
          : []
        this.context = { tab: parseTab(d.tab), adapters }
        this.handlers.context?.(this.context)
        return true
      }
      case 'response': {
        const id = typeof d.id === 'string' ? d.id : null
        const waiter = id ? this.pending.get(id) : undefined
        if (!waiter || !id) return true
        this.pending.delete(id)
        this.env.clearTimeout(waiter.timer)
        if (d.ok === true) waiter.resolve(d.result ?? null)
        else {
          const err = isObj(d.error) ? d.error : {}
          waiter.reject(new BrowserError((String(err.code ?? 'adapter_error') as ErrorCode), String(err.message ?? 'The tab could not answer.')))
        }
        return true
      }
      case 'event': {
        if (typeof d.tabId !== 'number' || typeof d.adapter !== 'string' || typeof d.event !== 'string') return true
        this.handlers.event?.({ tabId: d.tabId, adapter: d.adapter, event: d.event, data: d.data ?? null })
        return true
      }
      default:
        return true
    }
  }

  /** Fail everything still waiting (the page is leaving). */
  close() {
    for (const [id, w] of this.pending) {
      this.env.clearTimeout(w.timer)
      w.reject(new BrowserError('not_ready', 'The panel closed.'))
      this.pending.delete(id)
    }
  }
}

/** One app page's `browser` entry, as `GET /api/browser/apps` lists it. */
export interface BrowserDeclaration {
  adapter: string
  capabilities: readonly string[]
  events: readonly string[]
  matches: readonly string[]
}

/** What the sandboxed page may see of the tab: only while it matches the page's patterns. */
export function visibleContext(decl: BrowserDeclaration, ctx: Context | null): { tab: { url: string; title: string } } | null {
  const tab = ctx?.tab
  if (!tab || !matchesAny(decl.matches, tab.url)) return null
  return { tab: { url: tab.url, title: tab.title } }
}

/**
 * The gate between ONE app page and the link: what its `canopy.browser` may do, decided
 * from its declaration and the tab as the extension last described it — never from
 * anything the page says about itself.
 */
export class BrowserGate {
  private decl: BrowserDeclaration
  private link: Pick<ExtensionLink, 'context' | 'request' | 'open'>

  constructor(decl: BrowserDeclaration, link: Pick<ExtensionLink, 'context' | 'request' | 'open'>) {
    this.decl = decl
    this.link = link
  }

  context() {
    return visibleContext(this.decl, this.link.context)
  }

  /** A declared capability, on the tab now in front of the person, if it matches. */
  request(capability: unknown, args: unknown): Promise<unknown> {
    if (typeof capability !== 'string' || !this.decl.capabilities.includes(capability)) {
      return Promise.reject(new BrowserError('unknown_capability',
        `This page does not declare "${String(capability)}" in its manifest's browser entry.`))
    }
    if (args != null && !isObj(args)) return Promise.reject(new BrowserError('bad_request', 'args must be an object'))
    const tab = this.link.context?.tab
    if (!tab || !matchesAny(this.decl.matches, tab.url)) {
      return Promise.reject(new BrowserError('no_adapter', 'The tab in front of you is not one this page runs on.'))
    }
    return this.link.request(tab.id, this.decl.adapter, capability, (args ?? {}) as Record<string, unknown>)
  }

  /** Should this adapter event reach the page? Declared, from its adapter, about the tab in front. */
  admits(e: AdapterEvent): boolean {
    const tab = this.link.context?.tab
    return e.adapter === this.decl.adapter && this.decl.events.includes(e.event)
      && !!tab && e.tabId === tab.id && matchesAny(this.decl.matches, tab.url)
  }

  open(url: unknown): void {
    if (typeof url !== 'string') throw new BrowserError('bad_request', 'open() takes an https URL')
    this.link.open(url)
  }
}
