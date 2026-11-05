import { useEffect, useState } from 'react'
import { getSessionContext } from '@/api/chat'
import type { RefusalWords } from '@/components/chat/RefusalNotice'

/** What the held composer says when the preview refuses: the notice above it holds the why and the fix. */
export const COMPOSER_REFUSED = "You can't send a message here: the note above says why and how to fix it."

/**
 * Whether you could take a turn here, before you type: the Turn Context preview
 * (`GET /api/canopy-sessions/{id}/context`), which the same rule a real turn is resolved by
 * answers. The refusal when it says no; null when it says yes, is withheld, or could not be
 * read. A failed read never blocks the composer: the server still decides on send.
 *
 * `refresh` re-asks when something that could change the answer changes (your role here,
 * the session's content, its CLI).
 */
export function usePreviewRefusal(sessionId: string | undefined, refresh: unknown): RefusalWords | null {
  const [refusal, setRefusal] = useState<RefusalWords | null>(null)
  useEffect(() => {
    setRefusal(null)
    if (!sessionId) return
    let cancelled = false
    getSessionContext(sessionId)
      .then((out) => {
        if (cancelled) return
        const ctx = out.turn_context
        setRefusal(ctx && ctx.access === 'refused' && ctx.refusal ? ctx.refusal : null)
      })
      .catch(() => {
        /* the server decides on send; a preview that could not be read refuses nothing */
      })
    return () => {
      cancelled = true
    }
  }, [sessionId, refresh])
  return refusal
}
