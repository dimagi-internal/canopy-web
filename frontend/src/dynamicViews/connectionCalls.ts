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
 * - One click opens one lease ("operation"): the writes of that click reuse it until
 *   it expires, is used up or the screen changes. A write with neither a live lease
 *   nor a click is refused here, with no request.
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
export interface Operation {
  operation_id: string
  expires_at: string
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
}

const METHODS = new Set(['GET', 'POST', 'PATCH', 'DELETE'])
/** A lease covers at most this many writes (the server's bound). */
const MAX_WRITES = 500

export const NEEDS_CLICK = 'This needs a click.'

const matches = (line: DeclaredLine, method: string, path: string) => {
  if (line.method !== method) return false
  const want = String(line.path).split('/')
  const got = path.split('/')
  return want.length === got.length && want.every((w, i) => w === '*' || w === got[i])
}

interface Lease {
  key: string
  id: string
  expires: number
  used: number
}

const surfaceKey = (s: Surface) => (s.kind === 'view' ? `view:${s.id}:${s.version}` : `app:${s.workspace}:${s.key}`)

export interface CallOptions {
  /** The PAGE's own activation. Read at message receipt; absent means no click. */
  isActive?: () => boolean | undefined
  now?: () => number
}

export class ConnectionCalls {
  private lease: Lease | null = null
  private opening: { key: string; promise: Promise<Lease> } | null = null
  private inflight = new Set<AbortController>()
  private client: ConnectionClient
  private context: () => CallContext
  private isActive: () => boolean | undefined
  private now: () => number

  constructor(client: ConnectionClient, context: () => CallContext, opts: CallOptions = {}) {
    this.client = client
    this.context = context
    this.isActive = opts.isActive ?? (() => (typeof navigator === 'undefined' ? undefined : navigator.userActivation?.isActive))
    this.now = opts.now ?? Date.now
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

  private start(request: unknown): Promise<CallResult> {
    const clicked = this.isActive() === true // before anything else: it is transient
    const ctx = this.context()
    if (!ctx.canConnect) throw new Error("A public page can't use connections.")
    if (!ctx.surface) throw new Error('Save this view to use connections.')
    const surface = ctx.surface
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
    const lease = this.liveLease(key)
    if (lease) return this.send(provider, { ...base, operation_id: lease.id }, lease)
    let opening = this.opening?.key === key ? this.opening.promise : null
    if (!opening) {
      if (!clicked) throw new Error(NEEDS_CLICK)
      opening = this.open(surface, key)
    }
    return opening.then((l) => this.send(provider, { ...base, operation_id: l.id }, l))
  }

  private liveLease(key: string): Lease | null {
    const l = this.lease
    if (!l) return null
    if (l.key !== key || this.now() >= l.expires || l.used >= MAX_WRITES) {
      this.lease = null
      return null
    }
    return l
  }

  private open(surface: Surface, key: string): Promise<Lease> {
    const ctl = new AbortController()
    this.inflight.add(ctl)
    const promise = this.client
      .openOperation(surface, ctl.signal)
      .then((op) => {
        const lease = { key, id: op.operation_id, expires: Date.parse(op.expires_at), used: 0 }
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

  private send(provider: string, body: CallBody, lease?: Lease): Promise<CallResult> {
    if (lease) lease.used += 1
    const ctl = new AbortController()
    this.inflight.add(ctl)
    return this.client
      .request(provider, body, ctl.signal)
      .catch((e) => {
        if (lease && e instanceof ApiError && e.code === 'needs_operation' && this.lease === lease) this.lease = null
        throw refusal(e)
      })
      .finally(() => this.inflight.delete(ctl))
  }

  /** The screen is going away: abandon what is in flight and forget the lease. */
  closeAll(): void {
    for (const ctl of this.inflight) ctl.abort()
    this.inflight.clear()
    this.lease = null
    this.opening = null
  }
}

/** The error the frame is shown: the server's sentence, or a plain one for a bare 404. */
function refusal(e: unknown): Error {
  if (e instanceof ApiError) {
    const code = (e.body as { error?: unknown } | null | undefined)?.error
    if (e.status === 404 && !code) return new Error("This screen can't reach that.")
    return new Error(e.message)
  }
  return e instanceof Error ? e : new Error(String(e))
}
