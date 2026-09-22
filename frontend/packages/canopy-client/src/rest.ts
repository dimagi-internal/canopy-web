/**
 * Browser → canopy-web REST, with the bearer and the 401 retry in one place.
 *
 * Ported from ace-web's `frontend/src/canopy/api.ts`, minus everything that was
 * about ace-web: the `source: "ace-web"` literal, `aceOriginKey()`, and
 * `createCanopySession`, which called ace-web's OWN endpoint rather than
 * canopy's (session create stays host-side on purpose — the host stamps
 * `origin_key` server-side from a membership-checked path, so a caller cannot
 * claim another tenant's scope). Those become configuration and a host
 * callback respectively.
 *
 * Field mappings against canopy's real schemas are preserved from ace-web,
 * including the ones that were wrong once and got fixed there:
 *
 *  - `SessionOut` has no `updated_at`; it is `last_activity_at`.
 *  - `Runner.live_status` values are LOWERCASE (`online`, `stale`, …). An
 *    earlier ace-web draft compared against `"ONLINE"` — the Python constant's
 *    NAME, not its value — which made every runner look offline and mis-fired
 *    the placement banner on every chat.
 *  - `runner_online` is read off the session, not cross-referenced against the
 *    runner fleet: `GET /api/harness/runners/` is scoped to runners the caller
 *    personally PAIRED, so a delegated user sees an empty fleet there and could
 *    never otherwise distinguish a stalled chat from a slow one.
 */

import type { TokenStore } from './token'

export interface CanopySessionSummary {
  id: string
  title: string
  agentSlug: string | null
  updatedAt: string
  runnerName: string | null
  /** `true`/`false` when the session has a runner binding, `null` when it has
   *  none — there is nothing to be offline. */
  runnerOnline: boolean | null
}

export interface CanopySessionDetail extends CanopySessionSummary {
  hasMoreBefore: boolean
  oldestLoadedTurnIndex: number | null
}

/** `Runner.live_status`'s wire value for a reachable runner. Referenced as a
 *  constant rather than repeated, because the literal is what went wrong once. */
export const RUNNER_STATUS_ONLINE = 'online'

export interface RestConfig {
  /** canopy's browser-facing base — a path prefix (`/canopy`) or an absolute URL. */
  baseUrl: string
  tokens: TokenStore
  /** Stamped by the HOST server-side and used here only to FILTER the list to
   *  this product's sessions. Never sent on a create — a client that could set
   *  its own `origin_key` could read another tenant's chats. */
  originKey?: string
  /** `metadata.source` filter, e.g. `connect-labs`. */
  source?: string
  /** Injectable for tests and for a non-browser host. */
  fetchImpl?: typeof fetch
}

export class CanopyRestError extends Error {
  // Written out rather than declared as constructor parameter properties: the
  // app's tsconfig sets `erasableSyntaxOnly`, which forbids them. Nothing under
  // src/ imported this package until the embed entry did, so the whole package
  // was type-checked for the first time then — and did not compile.
  readonly status: number
  readonly path: string

  constructor(status: number, path: string) {
    super(`canopy request failed (${status}): ${path}`)
    this.status = status
    this.path = path
  }
}

export function createRest(config: RestConfig) {
  const doFetch = config.fetchImpl ?? ((...a: Parameters<typeof fetch>) => fetch(...a))

  async function raw(path: string, init: RequestInit = {}): Promise<Response> {
    const send = (bearer: string) =>
      doFetch(`${config.baseUrl}${path}`, {
        ...init,
        headers: {
          ...(init.body ? { 'Content-Type': 'application/json' } : {}),
          ...init.headers,
          Authorization: `Bearer ${bearer}`,
        },
      })

    let response = await send(await config.tokens.get())
    if (response.status === 401) {
      // Exactly one retry, with a FORCED refresh — canopy has rejected the
      // token, so our own expiry bookkeeping is not the authority here (it may
      // have been revoked early, or the clocks may disagree).
      response = await send(await config.tokens.get(true))
    }
    return response
  }

  async function json<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await raw(path, init)
    if (!response.ok) throw new CanopyRestError(response.status, path)
    if (response.status === 204) return undefined as T
    return (await response.json()) as T
  }

  function mapSummary(r: Record<string, unknown>): CanopySessionSummary {
    return {
      id: r.id as string,
      title: r.title as string,
      agentSlug: (r.agent_slug as string | null | undefined) ?? null,
      updatedAt: (r.last_activity_at as string | undefined) ?? (r.updated_at as string),
      runnerName: (r.runner_name as string | null | undefined) ?? null,
      runnerOnline: (r.runner_online as boolean | null | undefined) ?? null,
    }
  }

  /** Which surface this token reaches. Resolved AFTER a token exists, since
   *  the mint is what says whether it is for a user or a contact. */
  async function isContact(): Promise<boolean> {
    await config.tokens.get()
    return config.tokens.principal() === 'contact'
  }

  function filters(params: URLSearchParams): void {
    if (config.source) params.set('source', config.source)
    // Scopes the list to this product. Omitted when the host has no scope to
    // apply, in which case canopy's own per-principal filtering is the only limit.
    if (config.originKey) params.set('origin_key', config.originKey)
  }

  return {
    raw,
    json,

    /** `user` or `contact` — who this client is talking as. A contact is a
     *  first-class principal: someone the host vouched for who has no canopy
     *  account. Every method below routes to what they can reach. */
    async principal(): Promise<'user' | 'contact'> {
      return (await isContact()) ? 'contact' : 'user'
    },

    /** Agents this embedding app may offer — the picker's source. The app is
     *  resolved from the bearer token server-side, so there is deliberately no
     *  parameter here to get wrong. */
    async listAgents(): Promise<
      { slug: string; name: string; description: string; avatar_url?: string; workspace?: string }[]
    > {
      if (await isContact()) {
        const me = await json<{ agents: { slug: string; name: string; description: string }[] }>(
          '/api/contact/me',
        )
        return me.agents
      }
      return json('/api/embed/agents')
    },

    async listSessions(
      opts: { state?: string; agentSlug?: string } = {},
    ): Promise<CanopySessionSummary[]> {
      const params = new URLSearchParams()
      filters(params)
      if (await isContact()) {
        // A contact's list is their OWN conversations; `state` does not apply
        // (a contact's conversation is not archived by a runner report).
        const qs = params.toString()
        const rows = await json<Record<string, unknown>[]>(`/api/contact/sessions${qs ? `?${qs}` : ''}`)
        return rows
          .map(mapSummary)
          .filter((r) => !opts.agentSlug || r.agentSlug === opts.agentSlug)
      }
      if (opts.state) params.set('state', opts.state)
      if (opts.agentSlug) params.set('agent_slug', opts.agentSlug)
      const qs = params.toString()
      const rows = await json<Record<string, unknown>[]>(
        `/api/canopy-sessions/${qs ? `?${qs}` : ''}`,
      )
      return rows.map(mapSummary)
    },

    /** Single-session detail. NOT filtered by `state` or capped by a page limit,
     *  so it is the right call for "does THIS session have a bound runner" and
     *  "is there more history" — an archived or page-201st session vanishes from
     *  the list but is still directly gettable. */
    async getSession(id: string): Promise<CanopySessionDetail> {
      if (await isContact()) {
        const r = await json<Record<string, unknown>>(`/api/contact/sessions/${encodeURIComponent(id)}`)
        // A contact's detail carries no scroll-back cursor; `fetchOlder` answers
        // "is there more" itself, so report "maybe" and let the first page decide.
        return { ...mapSummary(r), hasMoreBefore: true, oldestLoadedTurnIndex: null }
      }
      const r = await json<Record<string, unknown>>(
        `/api/canopy-sessions/${encodeURIComponent(id)}`,
      )
      return {
        ...mapSummary(r),
        hasMoreBefore: Boolean(r.has_more_before),
        oldestLoadedTurnIndex: (r.oldest_loaded_turn_index as number | null | undefined) ?? null,
      }
    },

    /** `origin` is the routing source a USER's send may declare (e.g. `ace_web`);
     *  a contact's send is recorded as the site's widget by canopy itself. */
    async send(id: string, text: string, clientId: string, origin?: string): Promise<unknown> {
      if (await isContact()) {
        return json(`/api/contact/sessions/${encodeURIComponent(id)}/send`, {
          method: 'POST',
          body: JSON.stringify({ text, client_id: clientId }),
        })
      }
      return json(`/api/canopy-sessions/${encodeURIComponent(id)}/send`, {
        method: 'POST',
        body: JSON.stringify({ text, client_id: clientId, ...(origin ? { origin } : {}) }),
      })
    },

    /** The same page shape for both principals (canopy's `MessagePageOut`). */
    async fetchOlder(
      id: string,
      before: number,
    ): Promise<{ messages: unknown[]; has_more_before: boolean }> {
      const root = (await isContact()) ? '/api/contact/sessions' : '/api/canopy-sessions'
      return json(
        `${root}/${encodeURIComponent(id)}/messages?before=${encodeURIComponent(String(before))}`,
      )
    },

    // The viewer-liveness pair (`RunnerBinding.stream_desired`): attaching asks
    // the bound runner to stream this session live, detaching lets it stop once
    // the last viewer leaves. Best-effort by design — a caller fires these on
    // mount/unmount and must never block rendering on the result.
    async attach(id: string): Promise<void> {
      const root = (await isContact()) ? '/api/contact/sessions' : '/api/canopy-sessions'
      await raw(`${root}/${encodeURIComponent(id)}/attach`, { method: 'POST' })
    },
    async detach(id: string): Promise<void> {
      const root = (await isContact()) ? '/api/contact/sessions' : '/api/canopy-sessions'
      await raw(`${root}/${encodeURIComponent(id)}/detach`, { method: 'POST' })
    },
  }
}

export type CanopyRest = ReturnType<typeof createRest>
