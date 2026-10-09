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
import type { MemoryFeature, SessionMemory } from '@/api/people'
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
  }
}
