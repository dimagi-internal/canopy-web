/**
 * Agent memory for the conversation open in the widget — who may see it, and who
 * may change it.
 *
 * **Only the person changes it.** A site acting for its visitor is not the
 * visitor: if a host's delegated token could flip these, any site could switch
 * on learning for everyone who chats on it. So the write lives on
 * `/api/people/me/…`, which a delegated token cannot reach
 * (apps/tokens/delegation.py).
 *
 * That gives the two cases without asking which one this is:
 *
 * - **canopy's own widget** (same-origin). canopy's session cookie rides the
 *   frame's requests, so the person themself is signed in and the person route
 *   answers — real toggles, same as the chat page.
 * - **a connected site** (cross-origin). The token IS the identity, the person
 *   route is refused (403 `not_delegable`), and the widget falls back to the
 *   read-only embed route: the state, plus a link to change it in canopy.
 *
 * Anything else (not this visitor's session, a contact, a broken token) hides
 * the toggles — the panel says nothing rather than something wrong.
 */
import type { GrantDuration, MemoryFeature, SessionMemory } from '@/api/people'
import type { SessionMemorySource } from '@/components/chat/SessionMemoryToggles'

/** The subset of the frame's REST client this needs, so it is testable alone. */
export interface MemoryRest {
  json<T>(path: string, init?: RequestInit): Promise<T>
}

type EmbedSessionMemory = SessionMemory & { manage_path: string }

const status = (e: unknown) => (e as { status?: number } | null)?.status

export function embedMemorySource(
  rest: MemoryRest,
  sessionId: string,
  baseUrl: string,
): SessionMemorySource {
  const id = encodeURIComponent(sessionId)
  const personPath = `/api/people/me/sessions/${id}/agent-memory/`
  return {
    async load() {
      try {
        return { state: await rest.json<SessionMemory>(personPath), editable: true }
      } catch (e) {
        if (status(e) !== 403) return null
      }
      try {
        const r = await rest.json<EmbedSessionMemory>(`/api/embed/sessions/${id}/agent-memory`)
        const { manage_path, ...state } = r
        return { state, editable: false, manageUrl: `${baseUrl}${manage_path}` }
      } catch {
        return null
      }
    },
    save(change: Partial<Record<MemoryFeature, 'on' | 'off' | 'inherit'>>) {
      return rest.json<SessionMemory>(personPath, { method: 'PUT', body: JSON.stringify(change) })
    },
    // Only reachable signed in as the person (canopy's own widget); a site's token
    // is refused here exactly as for the toggles, so a host can never grant.
    grant(features: MemoryFeature[], duration: GrantDuration) {
      return rest.json<SessionMemory>(`/api/people/me/sessions/${id}/agent-grants/`, {
        method: 'POST',
        body: JSON.stringify({ features, duration, surface: 'widget' }),
      })
    },
  }
}

/** A contact's HCP, from a site canopy trusts for their email
 *  (`apps/tokens/contact_hcp_api.py`). The site that framed us holds the contact
 *  token, so every call also carries a single-use FRAME PROOF that only this
 *  frame can mint: the browser sends canopy's HttpOnly frame cookie and marks the
 *  request same-origin, neither of which the host page can do. One proof, one call. */
export interface ContactHcpState {
  eligible: boolean
  reason: string
  opted_in: boolean
  email: string
  site: string
  categories: string[]
  session_grant_hours: number
  policy: Partial<Record<MemoryFeature, { available: boolean; default: boolean }>>
  session: SessionMemory | null
  grants: { grant_id: string; agent: string; type: string; features: string[]; expires_at: string | null }[]
  entries?: { entry_id: string; category: string; statement: string; status: string }[]
}

export function contactHcp(rest: MemoryRest) {
  async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
    const { proof } = await rest.json<{ proof: string }>('/api/contact/hcp/proof', {
      method: 'POST',
    })
    return rest.json<T>(`/api/contact/hcp${path}`, {
      ...init,
      headers: { ...(init.headers ?? {}), 'X-Canopy-Frame-Proof': proof },
    })
  }
  const body = (b: unknown) => ({ body: JSON.stringify(b) })
  return {
    state: (sessionId?: string) =>
      call<ContactHcpState>(
        `/state${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`,
      ),
    optIn: (sessionId: string, use: boolean) =>
      call<ContactHcpState>('/opt-in', { method: 'POST', ...body({ session_id: sessionId, use }) }),
    grant: (sessionId: string, features: MemoryFeature[], duration: GrantDuration) =>
      call<ContactHcpState>(`/sessions/${encodeURIComponent(sessionId)}/agent-grants`, {
        method: 'POST',
        ...body({ features, duration }),
      }),
    sessionMemory: (sessionId: string, change: Partial<Record<MemoryFeature, 'on' | 'off' | 'inherit'>>) =>
      call<ContactHcpState>(`/sessions/${encodeURIComponent(sessionId)}/memory`, {
        method: 'PUT',
        ...body(change),
      }),
    policy: (change: Partial<Record<MemoryFeature, { available?: boolean; default?: boolean }>>) =>
      call<ContactHcpState>('/policy', { method: 'PUT', ...body(change) }),
    revoke: (grantId: string) =>
      call<ContactHcpState>(`/grants/${encodeURIComponent(grantId)}`, { method: 'DELETE' }),
    removeEntry: (entryId: string) =>
      call<ContactHcpState>(`/entries/${encodeURIComponent(entryId)}`, { method: 'DELETE' }),
    exportAll: () => call<unknown>('/export'),
  }
}

export type ContactHcp = ReturnType<typeof contactHcp>

/** The chat page's toggles, for an opted-in contact: same component, the
 *  contact's own routes. `null` until opted in (the opt-in comes first). */
export function contactMemorySource(hcp: ContactHcp, sessionId: string): SessionMemorySource {
  const session = (s: ContactHcpState) => {
    if (!s.session) throw new Error('no session state')
    return s.session
  }
  return {
    async load() {
      try {
        const s = await hcp.state(sessionId)
        if (!s.eligible || !s.opted_in || !s.session) return null
        return { state: s.session, editable: true }
      } catch {
        return null
      }
    },
    save: async (change) => session(await hcp.sessionMemory(sessionId, change)),
    grant: async (features, duration) => session(await hcp.grant(sessionId, features, duration)),
  }
}
