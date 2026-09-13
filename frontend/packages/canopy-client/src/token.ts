/**
 * The delegated-token cache.
 *
 * Ported from ace-web's `frontend/src/canopy/token.ts`, with one dependency
 * inverted and one global removed.
 *
 * **Inverted:** ace-web's version called ace-web's own `POST /api/canopy/token`
 * through ace-web's generated API client. That is the one part of the flow that
 * *must* stay host-specific — the `AppCredential` is a secret, so only the host
 * backend can mint — so the client takes a `fetchToken` callback and knows
 * nothing about how the host exchanges. That inversion is the whole reason this
 * package can serve a Django host, a React SPA, and an iframe widget at once.
 *
 * **De-globalised:** ace-web held `cached` and `inflight` at module scope, which
 * is fine for exactly one client per page. A widget can be mounted twice (two
 * panels, or a host embedding two agents), and tests then leak state into each
 * other. `createTokenStore` closes over its own state instead.
 */

/** What a host's token endpoint must return. `expiresAt` is opaque to the host
 *  contract but must be parseable by `Date` — canopy sends ISO-8601. */
export interface CanopyToken {
  token: string
  expiresAt: string
}

export type FetchToken = () => Promise<CanopyToken>

export interface TokenStore {
  /** Cached token, refetching when it is near expiry. `force` bypasses the
   *  cache outright — used by the 401 retry, which must not trust our own
   *  expiry bookkeeping when canopy has already rejected the token. */
  get(force?: boolean): Promise<string>
  /** Sync read for callers that cannot await — the WS URL builder, which has to
   *  put the token in a query string. `null` before the first mint. */
  peek(): string | null
  /** Drop the cached token. For a host that knows the user signed out. */
  clear(): void
}

/** Refetch this long before real expiry, so a request kicked off just under the
 *  wire does not race expiry mid-flight. */
const REFRESH_SKEW_MS = 5 * 60 * 1000

/** A non-parseable `expiresAt` is treated as already-expired rather than cached
 *  with `NaN`. `now < NaN - skew` is always false, so this was already forcing a
 *  refetch every call in ace-web; making it explicit means the behaviour is
 *  intended rather than a coincidence of NaN comparison. */
function expiresAtMs(expiresAt: string): number {
  const ms = new Date(expiresAt).getTime()
  return Number.isNaN(ms) ? 0 : ms
}

export function createTokenStore(fetchToken: FetchToken): TokenStore {
  let cached: { token: string; expiresAtMs: number } | null = null
  // In-flight dedup. Without it, several components mounting in the same tick
  // each call get() before any has a cached result, firing N concurrent mints —
  // and N new DelegatedToken rows server-side. Every caller in that tick awaits
  // the one request already underway.
  let inflight: Promise<string> | null = null

  return {
    get(force = false) {
      if (!force && cached && Date.now() < cached.expiresAtMs - REFRESH_SKEW_MS) {
        return Promise.resolve(cached.token)
      }
      if (!inflight) {
        inflight = fetchToken()
          .then(({ token, expiresAt }) => {
            cached = { token, expiresAtMs: expiresAtMs(expiresAt) }
            return token
          })
          .finally(() => {
            // Cleared even on rejection, so a transient failure does not poison
            // every later call with the same stale rejected promise.
            inflight = null
          })
      }
      return inflight
    },
    peek() {
      return cached ? cached.token : null
    },
    clear() {
      cached = null
    },
  }
}
