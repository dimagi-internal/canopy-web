import { useCallback, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import type { UseSessionSocketResult } from 'canopy-ui/chat'

import { ChatApiError, forkSession, type ChatSession } from '@/api/chat'
import type { RunnerOut } from '@/api/harness'
import { canFork, turnIndexAt } from '@/components/chat/MessageContextMenu'
import { scopedPath } from '@/lib/scopedLinks'

/** Right-click a message: "Fork from here". `sessionPath` is where the fork
 *  opens (/ide keeps it in /ide); the default is its chat page. */
export function useMessageFork(
  sessionId: string,
  runnerName: string | null | undefined,
  fleetRunners: RunnerOut[],
  messages: UseSessionSocketResult['state']['messages'],
  sessionPath?: (s: ChatSession) => string,
) {
  const navigate = useNavigate()
  const [messageMenu, setMessageMenu] = useState<{ x: number; y: number; turnIndex: number } | null>(null)
  const [forking, setForking] = useState(false)
  const [forkError, setForkError] = useState<string | null>(null)

  const closeMessageMenu = useCallback(() => {
    setMessageMenu(null)
    setForkError(null)
  }, [])

  const onMessageContextMenu = (e: ReactMouseEvent) => {
    // Only where a fork can be cut; elsewhere the browser's own menu stays.
    if (!canFork(runnerName, fleetRunners)) return
    const turnIndex = turnIndexAt(e.target)
    // A row not yet in the transcript (your own line, still sending) has a
    // placeholder index; leave the browser's own menu there.
    if (turnIndex == null || !messages.some((m) => m.turn_index === turnIndex && m.status !== 'pending')) {
      return
    }
    e.preventDefault()
    setForkError(null)
    setMessageMenu({ x: e.clientX, y: e.clientY, turnIndex })
  }

  const forkHere = async () => {
    if (!messageMenu || !sessionId) return
    setForking(true)
    setForkError(null)
    try {
      const fork = await forkSession(sessionId, messageMenu.turnIndex)
      setMessageMenu(null)
      navigate(
        sessionPath
          ? sessionPath(fork)
          : scopedPath(fork.workspace, `/chat/${encodeURIComponent(fork.id)}`),
      )
    } catch (err) {
      setForkError(err instanceof ChatApiError ? err.message : 'Could not fork this session')
    } finally {
      setForking(false)
    }
  }

  return { messageMenu, forking, forkError, closeMessageMenu, onMessageContextMenu, forkHere }
}
