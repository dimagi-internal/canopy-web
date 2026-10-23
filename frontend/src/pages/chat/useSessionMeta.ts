import { useEffect, useRef, useState } from 'react'

import { getAgent } from '@/api/agents'
import { getSession, type ChatSessionDetail } from '@/api/chat'
import { takeGateFetch } from '@/components/chat/PrivateConversationGate'

/** The session's REST detail, fetched once per id, plus the agent's display
 *  name for the header's "with <agent>" tag. `onLoaded` sees the first detail
 *  (the transcript pager seeds its scroll-back cursor from it). */
export function useSessionMeta(sessionId: string, onLoaded?: (m: ChatSessionDetail) => void) {
  const [meta, setMeta] = useState<ChatSessionDetail | null>(null)
  const [metaError, setMetaError] = useState<string | null>(null)
  // `meta.agent_slug` is the stable id, not something to show. A full agent
  // list isn't otherwise loaded on this standalone route, so this fetches just
  // the one.
  const [agentName, setAgentName] = useState<string | null>(null)
  const onLoadedRef = useRef(onLoaded)
  onLoadedRef.current = onLoaded

  useEffect(() => {
    if (!sessionId) return
    setMeta(null)
    setMetaError(null)
    setAgentName(null)
    // The private-conversation gate already read it to decide (PrivateConversationGate).
    ;(takeGateFetch(sessionId) ?? getSession(sessionId))
      .then((m) => {
        setMeta(m)
        onLoadedRef.current?.(m)
      })
      .catch((err: unknown) => {
        setMetaError(err instanceof Error ? err.message : 'session not found')
      })
  }, [sessionId])

  // Not re-fetched per session switch beyond that — `agentSlug` is stable for
  // the life of a session, so there is nothing to invalidate.
  const slug = meta?.agent_slug
  useEffect(() => {
    if (!slug) return
    let live = true
    getAgent(slug)
      .then((a) => { if (live) setAgentName(a.name) })
      // Same fallback as every other agent/slug join in this app (e.g.
      // ChatSessionsPanel's `agentName`): the slug reads fine on its own.
      .catch(() => { if (live) setAgentName(slug) })
    return () => { live = false }
  }, [slug])

  return { meta, setMeta, metaError, agentName }
}
