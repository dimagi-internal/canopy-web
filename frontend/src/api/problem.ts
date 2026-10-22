/**
 * The ONE way an API module turns a failed response into an error.
 *
 * Every backend error answers RFC 7807 `application/problem+json` (see
 * `apps/api/errors.py`): `{type, title, status, detail, instance, extras}`,
 * plus the odd top-level extension member (`{"error": "step_up_required"}`).
 * `detail` is the per-occurrence reason ("A schedule named 'X' already exists
 * for this agent."); `title` is the stable, per-`type` class name. Throwing a
 * hardcoded "Failed to create schedule" discards both, so the UI tells the user
 * that something failed but never why.
 *
 * `openapi-fetch` hands the PARSED error body back as its `error` value, but
 * types it off whatever error responses the schema happens to declare (often
 * none — django-ninja's HttpError raises are not declared response models, so
 * `error` is statically `undefined`). So narrow at runtime rather than trusting
 * the type — a non-JSON error (a proxy's HTML 502) lands here too and must fall
 * back cleanly.
 *
 * Use:
 *   - `unwrap(res, 'listAgents')` — the data, or throw an `ApiError`;
 *   - `ensureOk(res, 'Failed to delete')` — for a 204 / body-less success;
 *   - `ApiError.from(status, body, fallback)` — when you hold a raw Response.
 * Callers branch on `e instanceof ApiError && e.status === 404` (or `e.code`),
 * and show `e.message`, which is the server's `detail` whenever it sent one.
 */

/** The parsed problem body, as loosely as the server may send it. */
type ProblemBody = {
  type?: unknown
  title?: unknown
  detail?: unknown
  extras?: unknown
  error?: unknown
}

function asObject(body: unknown): ProblemBody | null {
  return body && typeof body === 'object' && !Array.isArray(body) ? (body as ProblemBody) : null
}

/**
 * The per-occurrence reason, or "" when there is none. Ninja sends a string
 * `detail` for an HttpError and a list of `{loc, msg, type}` for a schema
 * validation 422 — the first `msg` is the readable part of that.
 */
export function problemDetail(body: unknown): string {
  const raw = asObject(body)?.detail
  const first = Array.isArray(raw) ? ((asObject(raw[0]) as { msg?: unknown } | null)?.msg ?? raw[0]) : raw
  return typeof first === 'string' && first.trim() ? first : ''
}

/** The message a human should read: the detail, else the title, else `fallback`. */
export function problemMessage(error: unknown, fallback: string): string {
  const detail = problemDetail(error)
  if (detail) return detail
  // `detail` is optional in the Problem model; `title` is not. A bare
  // validation error may only carry title.
  const title = asObject(error)?.title
  if (typeof title === 'string' && title.trim()) return title
  return fallback
}

/**
 * A refused API call. `status` is the HTTP status (0 when there was no
 * response); `type`/`title`/`detail`/`extras` are the problem body's fields
 * ("" / undefined when absent); `code` is the machine-readable reason — a
 * top-level `error` member when the server sent one (`step_up_required`,
 * `no_vault_keyset`), else the tail of the `type` URI (`not-found`, `conflict`),
 * else "". `body` is the parsed error body exactly as received.
 */
export class ApiError extends Error {
  readonly status: number
  readonly type: string
  readonly code: string
  readonly title: string
  readonly detail: string
  readonly extras: Record<string, unknown> | undefined
  readonly body: unknown

  constructor(status: number, message: string, body?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
    const p = asObject(body)
    this.type = typeof p?.type === 'string' ? p.type : ''
    this.title = typeof p?.title === 'string' ? p.title : ''
    this.detail = problemDetail(body)
    this.extras = asObject(p?.extras) ?? undefined
    const member = typeof p?.error === 'string' ? p.error : ''
    this.code = member || this.type.split('/').pop() || ''
  }

  /** Build from a status + parsed body; the message is the server's own when it sent one. */
  static from(status: number, body: unknown, fallback: string): ApiError {
    return new ApiError(status, problemMessage(body, fallback), body)
  }
}

/** What an openapi-fetch call resolves to, loosely: enough to judge it. */
type FetchResult<T> = { data?: T; error?: unknown; response?: Response | { status: number; ok?: boolean } }

function statusOf(res: FetchResult<unknown>): number {
  return res.response?.status ?? 0
}

function failed(res: FetchResult<unknown>): boolean {
  const r = res.response
  if (r) return r.ok === undefined ? r.status < 200 || r.status >= 300 : !r.ok
  return res.error !== undefined
}

/**
 * The fallback message when the server says nothing: `what` verbatim when it is
 * a sentence ("Failed to load members"), else "<operation> failed (<status>)"
 * for an operation name ("listAgents").
 */
function fallbackFor(what: string, status: number): string {
  if (/\s/.test(what.trim())) return what
  return status ? `${what} failed (${status})` : `${what} failed`
}

/** Throw the `ApiError` for a failed call; a no-op on success. */
export function ensureOk(res: FetchResult<unknown>, what: string): void {
  if (failed(res)) {
    const status = statusOf(res)
    throw ApiError.from(status, res.error, fallbackFor(what, status))
  }
}

/**
 * The response data of a successful openapi-fetch call, or throw an `ApiError`
 * carrying the server's problem. A success with no body is an error here too —
 * use `ensureOk` for a route that answers 204.
 */
export function unwrap<T>(res: FetchResult<T>, what: string): T {
  ensureOk(res, what)
  if (res.data === undefined) {
    const status = statusOf(res)
    throw new ApiError(status, fallbackFor(what, status), res.error)
  }
  return res.data
}
