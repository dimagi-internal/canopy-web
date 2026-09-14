/**
 * Which kind of person is behind this frame.
 *
 * canopy has two principals now and the frame can hold a token for either: a
 * USER (canopy's own pages, or a host whose visitors have canopy accounts) or a
 * CONTACT (someone a connected site vouched for, with no canopy account at
 * all). They reach entirely different surfaces — `/api/embed/*` versus
 * `/api/contact/*` — so the frame has to know which it has before it asks
 * anything.
 *
 * **Discovered rather than configured, on purpose.** The obvious alternative is
 * an option on `canopy.init`, but that is a fact about the token the host's
 * backend minted, not about the page — so it would be a second place to state
 * something already true, and the failure when the two disagreed would be a
 * 401 with no hint that a config value was the cause. Probing costs one extra
 * request, and only on the contact path.
 *
 * The order is deliberate: the user surface is tried first because it is the
 * common case today (canopy's own widget), so the common case pays nothing.
 */

export interface EmbedAgent {
  slug: string
  name: string
  description: string
  avatar_url?: string
  workspace?: string
}

export type Principal =
  | { kind: 'user'; agents: EmbedAgent[] }
  | { kind: 'contact'; contactId: number; displayName: string; app: string; agents: EmbedAgent[] }

/** The subset of the REST client this needs, so it is testable without one. */
export type JsonFetch = <T>(path: string) => Promise<T>

/** Status codes that mean "wrong principal", as opposed to "broken". */
function isPrincipalMismatch(error: unknown): boolean {
  const status = (error as { status?: number } | null)?.status
  return status === 401 || status === 403
}

export async function resolvePrincipal(json: JsonFetch): Promise<Principal> {
  try {
    const agents = await json<EmbedAgent[]>('/api/embed/agents')
    return { kind: 'user', agents }
  } catch (error) {
    if (!isPrincipalMismatch(error)) throw error
  }

  // Not a user token. The contact surface answers for the other principal, and
  // if it refuses too then the token is simply not good — which reaches the
  // caller as the error it deserves rather than as an empty agent list.
  const me = await json<{
    contact_id: number
    display_name: string
    app: string
    agents: EmbedAgent[]
  }>('/api/contact/me')
  return {
    kind: 'contact',
    contactId: me.contact_id,
    displayName: me.display_name,
    app: me.app,
    agents: me.agents,
  }
}
