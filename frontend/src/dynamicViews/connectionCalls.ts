/**
 * A screen's calls to a connected account (Salesforce), made through canopy-web.
 *
 * A view's code runs in a sandboxed frame with no network, and is not trusted. It
 * asks `canopy.connection.request(...)`; the frame posts it to the page around it,
 * which holds the session and makes the call (`POST /api/connections/{provider}/request`,
 * apps/connections). So:
 *
 * - A WRITE needs a click, and the page decides that from its OWN
 *   `navigator.userActivation.isActive`, read synchronously when the message
 *   arrives (it is transient, so before any await). A click inside the frame
 *   activates this page too; script cannot set it. The frame's message has no click
 *   field at all, and anything in it claiming one is ignored. No such API: fail closed.
 * - One click opens one lease ("operation"): up to 10 minutes and 500 writes within what
 *   the screen declared. It ends 120 seconds after its last write, or when it is used up
 *   or the screen changes. A write with neither a live lease nor a click is refused here,
 *   with no request. `canopy.connection.begin()` opens it without writing yet.
 * - Calls wait their turn in one first-come queue for the page, at most 8 at once across
 *   its frames: a dashboard's load outnumbers what the server takes from one person.
 *   A write's lease is checked when its turn comes, not when it joined.
 * - A line the screen did not declare is refused here as well, as a courtesy. This is
 *   a declared-line match and nothing more: the server is the authority (allowlist,
 *   revision, whose connection), and refuses in a sentence the frame is shown as is.
 *
 * Free of React and of the network (the client is passed in): connectionCalls.test.ts.
 */
import { ApiError } from '@/api/problem'

export type Surface =
  | { kind: 'view'; id: number; version: number }
  | { kind: 'app'; workspace: string; key: string }

export interface DeclaredLine {
  method: string
  path: string
}
export interface DeclaredConnection {
  provider?: string
  as?: string
  agent?: string
  reads?: readonly DeclaredLine[]
  writes?: readonly DeclaredLine[]
}

export interface CallBody {
  method: string
  path: string
  query?: Record<string, string | string[]>
  body?: unknown
  surface: Surface
  as: 'viewer' | 'shared'
  agent?: string
  operation_id?: string
}
export interface CallResult {
  status: number
  body?: unknown
  text?: string | null
}
export interface LeaseWindow {
  expires_at: string
  hard_limit_at: string
  writes: readonly { provider: string; method: string; path: string }[]
}
export interface Operation extends LeaseWindow {
  operation_id: string
}

export interface ConnectionClient {
  request(provider: string, body: CallBody, signal?: AbortSignal): Promise<CallResult>
  openOperation(surface: Surface, signal?: AbortSignal): Promise<Operation>
}

/** What the screen is, read fresh on each call. */
export interface CallContext {
  /** Null: a preview with no saved version, which has no revision to check against. */
  surface: Surface | null
  /** The screen's `connections`. Undefined: not known here (an app's), and the server decides. */
  declared: readonly DeclaredConnection[] | undefined
  /** False on every door but a signed-in member's. */
  canConnect: boolean
  /** An app draft's test copy: never calls. */
  draft?: boolean
}

const METHODS = new Set(['GET', 'POST', 'PATCH', 'DELETE'])
/** A lease covers at most this many writes (the server's bound). */
const MAX_WRITES = 500
/** A lease ends this long after its last write... (the server's `OPERATION_SECONDS`) */
const LEASE_IDLE_MS = 120_000
/** ...and this long after its click, whatever is written (`OPERATION_HARD_LIMIT_SECONDS`). */
const LEASE_HARD_LIMIT_MS = 600_000
/** Calls going at once from one page, under the server's 10 per person. */
export const MAX_IN_FLIGHT = 8
/** How long a call may wait for its turn. */
export const QUEUE_WAIT_MS = 60_000
export const QUEUE_GAVE_UP = 'This page has too many calls waiting, and this one waited a minute. Try again in a moment.'

/**
 * Was there a click on THIS screen just now? The page is activated by a click in any frame
 * (or on canopy itself), so activation alone could be spent by a sibling view or a timer. Also
 * require that this page has focus and that the focused element is this screen's iframe.
 */
export function pageClickedFrame(
  frame: Element | null,
  env: {
    nav?: { userActivation?: { isActive?: boolean } }
    doc?: { hasFocus(): boolean; activeElement: Element | null | unknown }
  } = {
    nav: typeof navigator === 'undefined' ? undefined : navigator,
    doc: typeof document === 'undefined' ? undefined : document,
  },
): boolean {
  if (!frame || !env.doc) return false
  return env.nav?.userActivation?.isActive === true && env.doc.hasFocus() && env.doc.activeElement === frame
}

export const NEEDS_CLICK = 'This needs a click.'

const matches = (line: DeclaredLine, method: string, path: string) => {
  if (line.method !== method) return false
  const want = String(line.path).split('/')
  const got = path.split('/')
  return want.length === got.length && want.every((w, i) => w === '*' || w === got[i])
}

/**
 * Time is measured from this browser's clock, from when the answer came, never against the server's
 * `expires_at`: the two clocks can differ by anything, and the server has the last word on each write.
 */
interface Lease {
  key: string
  id: string
  opened: number
  lastWrite: number
  used: number
  window: LeaseWindow
}

interface Waiting {
  owner: object
  go: () => void
  fail: (e: Error) => void
  timer: ReturnType<typeof setTimeout>
}

/** One first-come line for a page's calls. Each frame's `ConnectionCalls` joins the same one. */
export class CallQueue {
  private waiting: Waiting[] = []
  private flying = new Map<symbol, object>()

  /** Run `start` when a place is free, or reject with a sentence after `QUEUE_WAIT_MS`. */
  run<T>(owner: object, start: () => Promise<T>): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const go = () => {
        const place = Symbol('place')
        this.flying.set(place, owner)
        const leave = () => {
          if (this.flying.delete(place)) this.next()
        }
        let started: Promise<T>
        try {
          started = start()
        } catch (e) {
          started = Promise.reject(e)
        }
        started.then(resolve, reject).finally(leave)
      }
      if (this.flying.size < MAX_IN_FLIGHT && !this.waiting.length) return go()
      const item: Waiting = {
        owner, go, fail: reject,
        timer: setTimeout(() => {
          this.waiting = this.waiting.filter((w) => w !== item)
          reject(new Error(QUEUE_GAVE_UP))
        }, QUEUE_WAIT_MS),
      }
      this.waiting.push(item)
    })
  }

  private next(): void {
    while (this.flying.size < MAX_IN_FLIGHT && this.waiting.length) {
      const item = this.waiting.shift()!
      clearTimeout(item.timer)
      item.go()
    }
  }

  /** This screen is going away: what it has waiting is refused, and what it has going gives up its place. */
  drop(owner: object): void {
    const mine = this.waiting.filter((w) => w.owner === owner)
    this.waiting = this.waiting.filter((w) => w.owner !== owner)
    for (const item of mine) {
      clearTimeout(item.timer)
      item.fail(new Error('This screen closed.'))
    }
    for (const [place, who] of this.flying) if (who === owner) this.flying.delete(place)
    this.next()
  }
}

/** The page's own line: everything in this document shares it. */
const pageQueue = new CallQueue()

const surfaceKey = (s: Surface) => (s.kind === 'view' ? `view:${s.id}:${s.version}` : `app:${s.workspace}:${s.key}`)

export interface CallOptions {
  /** A click on this screen, judged by the PAGE (`pageClickedFrame`). Read at message receipt; absent means no click. */
  isActive?: () => boolean | undefined
  now?: () => number
  /** The page's line unless a test brings its own. */
  queue?: CallQueue
}

export class ConnectionCalls {
  private lease: Lease | null = null
  private opening: { key: string; promise: Promise<Lease> } | null = null
  private inflight = new Set<AbortController>()
  private client: ConnectionClient
  private context: () => CallContext
  private isActive: () => boolean | undefined
  private now: () => number
  readonly queue: CallQueue

  constructor(client: ConnectionClient, context: () => CallContext, opts: CallOptions = {}) {
    this.client = client
    this.context = context
    this.isActive = opts.isActive ?? (() => false)
    this.now = opts.now ?? Date.now
    this.queue = opts.queue ?? pageQueue
  }

  /**
   * Answer one call from the frame. Everything up to the first await runs inside
   * the message event, which is where a click is still live.
   */
  handle(request: unknown): Promise<CallResult> {
    try {
      return this.start(request)
    } catch (e) {
      return Promise.reject(e)
    }
  }

  /**
   * `canopy.connection.begin()`: this click's lease, opened before any write, so a screen can read and
   * dedup first and still write on it. The frame gets its limits, never its id. Like a write, it runs up
   * to its first await inside the message event, and a new click opens a new window.
   */
  begin(): Promise<LeaseWindow> {
    try {
      const clicked = this.isActive() === true // before anything else: it is transient
      const surface = this.usableSurface()
      if (!clicked) throw new Error(NEEDS_CLICK)
      const key = surfaceKey(surface)
      const opening = this.opening?.key === key ? this.opening.promise : this.open(surface, key)
      return opening.then((l) => l.window)
    } catch (e) {
      return Promise.reject(e)
    }
  }

  /** Whether this door may call at all: the same answer for a call and for `begin`. */
  private usableSurface(): Surface {
    const ctx = this.context()
    if (ctx.draft) throw new Error("A draft can't use connections. Publish it first.")
    if (!ctx.canConnect) throw new Error("A public page can't use connections.")
    if (!ctx.surface) throw new Error('Save this view to use connections.')
    return ctx.surface
  }

  private start(request: unknown): Promise<CallResult> {
    const clicked = this.isActive() === true // before anything else: it is transient
    const surface = this.usableSurface()
    const ctx = this.context()
    if (!request || typeof request !== 'object' || Array.isArray(request)) throw new Error('connection.request takes an object.')
    // Only these fields; whatever else the frame's message carried (a claimed click, an
    // operation of its own making) is dropped here, never forwarded.
    const r = request as Record<string, unknown>
    const provider = typeof r.provider === 'string' ? r.provider : ''
    const method = typeof r.method === 'string' ? r.method.toUpperCase() : ''
    const path = typeof r.path === 'string' ? r.path : ''
    if (!provider) throw new Error('connection.request needs a provider.')
    if (!METHODS.has(method)) throw new Error('connection.request needs a method: GET, POST, PATCH or DELETE.')
    if (!path) throw new Error('connection.request needs a path.')
    if (r.query != null && (typeof r.query !== 'object' || Array.isArray(r.query))) throw new Error('query must be an object.')
    const as = r.as === 'shared' ? 'shared' : 'viewer'
    const agent = typeof r.agent === 'string' && r.agent ? r.agent : undefined

    let isRead = true
    if (ctx.declared) {
      const entry = ctx.declared.find((e) => e.provider === provider && (e.as ?? 'viewer') === as && (as === 'viewer' || e.agent === agent))
      const read = !!entry?.reads?.some((l) => matches(l, method, path))
      const write = !read && !!entry?.writes?.some((l) => matches(l, method, path))
      if (!read && !write) throw new Error("This screen didn't declare that call.")
      isRead = read
    } else {
      isRead = false // unknown here: treat anything but GET as a write
    }
    if (method === 'GET') isRead = true

    const base: CallBody = {
      method, path, surface, as,
      ...(r.query != null ? { query: r.query as CallBody['query'] } : {}),
      ...(r.body !== undefined ? { body: r.body } : {}),
      ...(agent ? { agent } : {}),
    }
    if (isRead) return this.send(provider, base)

    const key = surfaceKey(surface)
    if (this.liveLease(key)) return this.send(provider, base, key)
    let opening = this.opening?.key === key ? this.opening.promise : null
    if (!opening) {
      if (!clicked) throw new Error(NEEDS_CLICK)
      opening = this.open(surface, key)
    }
    return opening.then(() => this.send(provider, base, key))
  }

  private liveLease(key: string): Lease | null {
    const l = this.lease
    if (!l || l.key !== key || l.used >= MAX_WRITES) return null
    const t = this.now()
    return t < l.lastWrite + LEASE_IDLE_MS && t < l.opened + LEASE_HARD_LIMIT_MS ? l : null
  }

  private open(surface: Surface, key: string): Promise<Lease> {
    const ctl = new AbortController()
    this.inflight.add(ctl)
    const promise = this.client
      .openOperation(surface, ctl.signal)
      .then((op) => {
        const at = this.now()
        const { expires_at, hard_limit_at, writes } = op
        const lease = { key, id: op.operation_id, opened: at, lastWrite: at, used: 0, window: { expires_at, hard_limit_at, writes } }
        this.lease = lease
        return lease
      })
      .catch((e) => {
        throw refusal(e)
      })
      .finally(() => {
        this.inflight.delete(ctl)
        if (this.opening?.promise === promise) this.opening = null
      })
    this.opening = { key, promise }
    return promise
  }

  /** `leaseKey`: this is a write on that surface, so it takes its turn and then needs a live lease. */
  private send(provider: string, body: CallBody, leaseKey?: string): Promise<CallResult> {
    return this.queue.run(this, () => {
      let lease: Lease | null = null
      if (leaseKey !== undefined) {
        lease = this.liveLease(leaseKey)
        if (!lease) throw new Error(NEEDS_CLICK)
        lease.used += 1
        lease.lastWrite = this.now()
      }
      const ctl = new AbortController()
      this.inflight.add(ctl)
      return this.client
        .request(provider, lease ? { ...body, operation_id: lease.id } : body, ctl.signal)
        .catch((e) => {
          if (lease && e instanceof ApiError && e.code === 'needs_operation' && this.lease === lease) this.lease = null
          throw refusal(e)
        })
        .finally(() => this.inflight.delete(ctl))
    })
  }

  /** The screen is going away: abandon what is in flight and forget the lease. */
  closeAll(): void {
    for (const ctl of this.inflight) ctl.abort()
    this.inflight.clear()
    this.queue.drop(this)
    this.lease = null
    this.opening = null
  }
}

/** What a view's promise rejects with, past its sentence: the server's code, and where to connect. */
export class ConnectionRefusal extends Error {
  error?: string
  connect_url?: string
}

/** The members of a refusal that travel to the frame with its sentence. */
export function refusalMembers(e: unknown): { code?: string; connect_url?: string } {
  if (!(e instanceof ConnectionRefusal)) return {}
  return { ...(e.error ? { code: e.error } : {}), ...(e.connect_url ? { connect_url: e.connect_url } : {}) }
}

/** The error the frame is shown: the server's sentence, or a plain one for a bare 404. */
function refusal(e: unknown): Error {
  if (e instanceof ApiError) {
    const body = (e.body ?? {}) as { error?: unknown; connect_url?: unknown }
    if (e.status === 404 && !body.error) return new Error("This screen can't reach that.")
    const out = new ConnectionRefusal(e.message)
    if (typeof body.error === 'string') out.error = body.error
    if (typeof body.connect_url === 'string') out.connect_url = body.connect_url
    return out
  }
  return e instanceof Error ? e : new Error(String(e))
}
