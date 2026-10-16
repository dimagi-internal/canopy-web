/**
 * Passkey step-up, for every call through the API client (canopy-web#1538).
 *
 * A route that needs a fresh passkey answers 403 problem+json with a top-level
 * `error: "step_up_required"` (`apps/passkeys/stepup.py`). The middleware below
 * catches exactly that, asks for the passkey, and RETRIES the original request
 * once — so a caller (Save a credential, Reveal, Approve a device key) is
 * written as if step-up did not exist.
 *
 * Asking is a dialog, not a bare `navigator.credentials.get()`: Safari only
 * lets WebAuthn run from a user gesture, and the gesture that started this
 * request has long expired by the time the 403 comes back. The dialog's button
 * is a fresh gesture. `StepUpDialog` registers itself as the handler; with no
 * handler mounted (a test, the embedded widget) the 403 passes through untouched.
 *
 * Deliberately imports nothing from `src/api/` — `client.v2.ts` imports this.
 */
import type { Middleware } from 'openapi-fetch'

export const STEP_UP_REQUIRED = 'step_up_required'

export interface StepUpRequest {
  /** False when the person has no passkey yet — the dialog offers to register one. */
  hasPasskey: boolean
  /** The server's explanation, shown in the dialog. */
  detail?: string
}

/** Resolves once a step-up succeeded; rejects if the person cancelled or it failed. */
export type StepUpHandler = (req: StepUpRequest) => Promise<void>

let handler: StepUpHandler | null = null
let pending: Promise<void> | null = null

export function setStepUpHandler(next: StepUpHandler | null): void {
  handler = next
}

/** Run the step-up UI, sharing one prompt between concurrent callers. */
export function requestStepUp(req: StepUpRequest): Promise<void> {
  if (!handler) return Promise.reject(new Error('No passkey prompt is available here'))
  if (!pending) {
    pending = handler(req).finally(() => {
      pending = null
    })
  }
  return pending
}

/** Pure: does this parsed body ask for a step-up? */
export function stepUpFromBody(status: number, body: unknown): StepUpRequest | null {
  if (status !== 403 || !body || typeof body !== 'object') return null
  const b = body as { error?: unknown; has_passkey?: unknown; detail?: unknown }
  if (b.error !== STEP_UP_REQUIRED) return null
  return { hasPasskey: b.has_passkey === true, detail: typeof b.detail === 'string' ? b.detail : undefined }
}

async function stepUpFromResponse(response: Response): Promise<StepUpRequest | null> {
  if (response.status !== 403) return null
  const type = response.headers.get('Content-Type') ?? ''
  if (!type.includes('json')) return null
  try {
    return stepUpFromBody(response.status, await response.clone().json())
  } catch {
    return null
  }
}

const SAFE = new Set(['GET', 'HEAD', 'OPTIONS'])

/**
 * Wrap any request-issuing thunk: on a step-up refusal, prompt and call it once
 * more. For raw `fetch` callers outside `apiV2` (which already gets this from
 * the middleware). `thunk` must be safe to repeat.
 */
export async function withStepUp(thunk: () => Promise<Response>): Promise<Response> {
  const first = await thunk()
  const ask = await stepUpFromResponse(first)
  if (!ask || !handler) return first
  try {
    await requestStepUp(ask)
  } catch {
    return first
  }
  return thunk()
}

/** The openapi-fetch middleware. Register it LAST, so its request copy carries
 *  every header the earlier middleware added (CSRF) and the final URL. */
export function stepUpMiddleware(): Middleware {
  const copies = new Map<string, Request>()
  return {
    onRequest({ request, id }) {
      // A body can be read once; keep an unread copy of anything that writes.
      if (!SAFE.has(request.method)) copies.set(id, request.clone())
      return undefined
    },
    async onResponse({ response, id }) {
      const copy = copies.get(id)
      copies.delete(id)
      if (!copy || !handler) return undefined
      const ask = await stepUpFromResponse(response)
      if (!ask) return undefined
      try {
        await requestStepUp(ask)
      } catch {
        return undefined // cancelled or failed: the caller sees the original 403
      }
      // Retried once, outside the middleware chain — a second refusal is final.
      return globalThis.fetch(copy)
    },
    onError({ id }) {
      copies.delete(id)
      return undefined
    },
  }
}
