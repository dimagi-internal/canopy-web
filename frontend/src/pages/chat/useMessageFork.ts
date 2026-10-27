import { useCallback, useMemo, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  pairToolMessages,
  toolCopyText,
  type Message,
  type MessageActionsHost,
  type UseSessionSocketResult,
} from 'canopy-ui/chat'

import { ChatApiError, forkSession, type ChatSession } from '@/api/chat'
import type { RunnerOut } from '@/api/harness'
import { canFork, turnIndexAt } from '@/components/chat/MessageContextMenu'
import { scopedPath } from '@/lib/scopedLinks'

type MessageMenu = { x: number; y: number; turnIndex: number; copyText: string; canFork: boolean }

/** What Copy takes from the row at `turnIndex`: a tool call's command and
 *  output, else the line's own text (a reply's markdown source). */
export function copyTextAt(messages: readonly Message[], turnIndex: number): string {
  for (const row of pairToolMessages([...messages])) {
    if (row.kind === 'tool_pair' && row.use.turn_index === turnIndex) return toolCopyText(row.use, row.result)
    if (row.kind === 'message' && row.message.turn_index === turnIndex) return row.message.plaintext ?? ''
  }
  return ''
}

/** A row the transcript already holds — your own line still sending has a
 *  placeholder index, and nothing can be cut there. */
function inTranscript(m: Pick<Message, 'status' | 'id'>): boolean {
  return m.status !== 'pending' && !String(m.id ?? '').startsWith('local:')
}

/** A message's actions, from its hover bar or its right-click menu: Copy, and
 *  "Fork from here" where the runner can cut one. `sessionPath` is where the
 *  fork opens (/ide keeps it in /ide); the default is its chat page. */
export function useMessageFork(
  sessionId: string,
  runnerName: string | null | undefined,
  fleetRunners: RunnerOut[],
  messages: UseSessionSocketResult['state']['messages'],
  sessionPath?: (s: ChatSession) => string,
) {
  const navigate = useNavigate()
  const [messageMenu, setMessageMenu] = useState<MessageMenu | null>(null)
  const [forking, setForking] = useState(false)
  const [forkError, setForkError] = useState<string | null>(null)
  const runnerCanFork = canFork(runnerName, fleetRunners)

  const closeMessageMenu = useCallback(() => {
    setMessageMenu(null)
    setForkError(null)
  }, [])

  const onMessageContextMenu = (e: ReactMouseEvent) => {
    // Selected text keeps the browser's own menu (its Copy is the one wanted).
    if (window.getSelection?.()?.toString()) return
    const turnIndex = turnIndexAt(e.target)
    const row = turnIndex == null ? undefined : messages.find((m) => m.turn_index === turnIndex)
    if (turnIndex == null || !row || !inTranscript(row)) return
    const copyText = copyTextAt(messages, turnIndex)
    if (!copyText && !runnerCanFork) return
    e.preventDefault()
    setForkError(null)
    setMessageMenu({ x: e.clientX, y: e.clientY, turnIndex, copyText, canFork: runnerCanFork })
  }

  const run = async (turnIndex: number) => {
    if (!sessionId) return
    setForking(true)
    setForkError(null)
    try {
      const fork = await forkSession(sessionId, turnIndex)
      setMessageMenu(null)
      navigate(
        sessionPath
          ? sessionPath(fork)
          : scopedPath(fork.workspace, `/sessions/${encodeURIComponent(fork.id)}`),
      )
    } catch (err) {
      setForkError(err instanceof ChatApiError ? err.message : 'Could not fork this session')
    } finally {
      setForking(false)
    }
  }

  const forkHere = async () => {
    if (messageMenu) await run(messageMenu.turnIndex)
  }

  // The hover bar's Fork runs at once; the menu opens beside the button only to
  // show progress and, if the server refuses, why.
  const messageActions = useMemo<MessageActionsHost>(
    () => ({
      canFork: (m) => runnerCanFork && inTranscript(m),
      onFork: (m, anchor) => {
        setMessageMenu({ ...anchor, turnIndex: m.turn_index, copyText: '', canFork: true })
        void run(m.turn_index)
      },
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [runnerCanFork, sessionId, sessionPath],
  )

  return { messageMenu, forking, forkError, closeMessageMenu, onMessageContextMenu, forkHere, messageActions }
}
