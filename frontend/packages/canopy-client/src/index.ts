/**
 * `@canopy/client` — layer 1 of the embedded-agent SDK (v2 spec §1).
 *
 * The transport half of talking to canopy-web, with **no framework and no
 * dependencies at all**. That emptiness is the feature: canopy-ui requires
 * React 19, and the hosts that most want an agent cannot always have it —
 * connect-labs is React 18, Django templates, alpine and htmx. The iframe
 * widget bundles its own React; a native React host brings its own; a Django
 * page brings none. All three use this.
 *
 * Extracted from ace-web's `frontend/src/canopy/{token,api,ws}.ts`, which was
 * already framework-free (zero React imports across ~390 lines) but trapped
 * inside one host and coupled to that host's endpoints. ace-web still has its
 * own copy; adopting this is a follow-up in that repo, and until it does the
 * duplication is real.
 *
 * What stays OUT, and why:
 *
 *  - **Token minting.** The `AppCredential` is a secret, so only a host backend
 *    can exchange it. The client takes a `fetchToken` callback.
 *  - **Session creation.** The host stamps `origin_key` server-side from a
 *    membership-checked path; a client that could set its own would be able to
 *    claim another tenant's scope. So creation is the host's endpoint, and this
 *    package only ever reads and sends.
 */

export { createTokenStore } from './token'
export type { CanopyToken, FetchToken, TokenStore } from './token'

export { buildSessionWsUrl } from './ws'
export type { WsLocation } from './ws'

export { createRest, CanopyRestError, RUNNER_STATUS_ONLINE } from './rest'
export type { CanopyRest, CanopySessionDetail, CanopySessionSummary, RestConfig } from './rest'

export { createHostBridge, UnknownActionError } from './bridge'
export type { ContextProvider, HostAction, HostBridge, HostContext } from './bridge'

import { createRest, type CanopyRest } from './rest'
import { createTokenStore, type FetchToken } from './token'
import { buildSessionWsUrl } from './ws'

export interface CanopyClientConfig {
  /** Browser-facing canopy base: a same-origin path prefix (`/canopy`) or an
   *  absolute URL (the normal case for an embedded widget). */
  baseUrl: string
  /** How this host mints a delegated token for the signed-in user. */
  fetchToken: FetchToken
  /** `metadata.origin_key` to FILTER the session list by — this product's
   *  scope, as the host stamped it server-side. */
  originKey?: string
  /** `metadata.source` to filter by, e.g. `connect-labs`. */
  source?: string
  fetchImpl?: typeof fetch
}

export interface CanopyClient {
  rest: CanopyRest
  /** The session socket URL, with the cached token already on it. Returns
   *  `null` when no token has been minted yet — the caller should not open a
   *  socket in that state, and getting `null` is easier to handle correctly
   *  than a URL that will be rejected. */
  sessionSocketUrl(sessionId: string): string | null
  /** Force the next request to re-mint. For a host that knows the user's
   *  identity changed (a sign-out, an account switch). */
  invalidateToken(): void
}

export function createCanopyClient(config: CanopyClientConfig): CanopyClient {
  const tokens = createTokenStore(config.fetchToken)
  const rest = createRest({
    baseUrl: config.baseUrl,
    tokens,
    originKey: config.originKey,
    source: config.source,
    fetchImpl: config.fetchImpl,
  })

  return {
    rest,
    sessionSocketUrl(sessionId) {
      const token = tokens.peek()
      if (!token) return null
      return buildSessionWsUrl(config.baseUrl, sessionId, token)
    },
    invalidateToken() {
      tokens.clear()
    },
  }
}
