import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import { useNavigate } from 'react-router-dom'

import {
  ChatApiError,
  closeSession,
  detachSessionFromSlack,
  getSession,
  moveSession,
  resetSession,
  setSessionNotify,
  setSessionTitle,
  type ChatSessionDetail,
} from '@/api/chat'
import { getSessionWorkspacePreference } from '@/api/sessionWorkspace'
import {
  CLOSE_POLL_MS,
  CLOSE_WAIT_MS,
  closeDestination,
  closeIntent,
  closeResultMessage,
} from '@/components/chat/closeAction'
import type { MoveTarget } from '@/components/chat/MoveWorkspaceForm'
import { scopedPath } from '@/lib/scopedLinks'

export interface SessionActionsOptions {
  sessionId: string
  meta: ChatSessionDetail | null
  setMeta: Dispatch<SetStateAction<ChatSessionDetail | null>>
  /** Replaces the page's own navigation on close/move (/ide's right pane). */
  onLeave?: () => void
  /** A rename landed here. */
  onTitleChange?: () => void
  /** Open with the title already being renamed. */
  editTitle?: boolean
}

/** What the ⋯ menu and the title do to a session: rename, rebuild from the
 *  runner's transcript, close, disconnect from Slack, move to another
 *  workspace, and the notify-on-every-completion toggle. */
export function useSessionActions({
  sessionId,
  meta,
  setMeta,
  onLeave,
  onTitleChange,
  editTitle = false,
}: SessionActionsOptions) {
  const navigate = useNavigate()
  const onTitleChangeRef = useRef(onTitleChange)
  onTitleChangeRef.current = onTitleChange

  // An owner/editor has a composer (and a title, a Slack binding) to manage; a
  // viewer does not. Same gate as the server's.
  const canManage = meta?.my_role === 'owner' || meta?.my_role === 'editor'

  // --- rename
  const [renaming, setRenaming] = useState(editTitle)
  const renameTo = useCallback(async (next: string) => {
    if (!sessionId) return
    const s = await setSessionTitle(sessionId, next)
    setMeta((prev) => (prev ? { ...prev, title: s.title } : prev))
    setRenaming(false)
    onTitleChangeRef.current?.()
  }, [sessionId, setMeta])

  // --- reset: drop canopy's derived copy and rebuild it from the runner's
  // transcript. Cheap and repeatable: the rows are a cache of a file on the
  // runner's disk, so this is the way to pick up anything that happened
  // outside a turn.
  const [resetting, setResetting] = useState(false)
  const [resetNote, setResetNote] = useState('')
  const resetFromTranscript = useCallback(async () => {
    if (!sessionId) return
    setResetting(true)
    setResetNote('')
    try {
      const res = await resetSession(sessionId)
      setResetNote(
        res.ok
          ? `Reset — rebuilding ${res.rows_dropped} message(s) from ${res.runner}`
          : res.reason === 'runner_unreachable'
            ? 'Its runner is offline — try again when it is back'
            : 'Nothing to rebuild from: this session has no runner transcript',
      )
    } catch (err) {
      setResetNote(err instanceof ChatApiError ? err.message : 'Reset failed')
    } finally {
      setResetting(false)
    }
  }, [sessionId])

  // --- close. Navigate away only when the session is REALLY gone. When the
  // close was relayed to a runner (`closing: true`), the emdash task is still
  // the truth until the runner's next report retires the session, so wait for
  // that and THEN leave. Leaving on the relay alone would claim a result we do
  // not have yet, and staying put for good left you on a dead chat with no way
  // back (Jonathan, 2026-09-18: "it should return me to the previous page").
  const [closing, setClosing] = useState(false)
  const [closeNote, setCloseNote] = useState<string | null>(null)
  const closeThisSession = useCallback(async () => {
    if (!meta) return
    const intent = closeIntent(meta)
    if (intent.kind === 'blocked') {
      setCloseNote(intent.why)
      return
    }
    if (intent.confirm && !window.confirm('This chat is still working. Close it anyway?')) return
    const leave = () => {
      if (onLeave) return onLeave()
      const to = closeDestination(window.history.state?.idx, meta.workspace)
      if (to === -1) navigate(-1)
      else navigate(to, { replace: true })
    }
    const closingId = sessionId
    setClosing(true)
    setCloseNote(null)
    try {
      const result = await closeSession(closingId)
      const message = closeResultMessage(result, meta)
      if (message) {
        setCloseNote(message)
        return
      }
      if (result.closing) {
        setCloseNote('Closing on the runner…')
        const deadline = Date.now() + CLOSE_WAIT_MS
        while (Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, CLOSE_POLL_MS))
          // Stop if you navigated elsewhere while waiting.
          if (!window.location.pathname.includes(closingId)) return
          const s = await getSession(closingId).catch(() => null)
          if (s && s.status !== 'active') {
            leave()
            return
          }
        }
        setCloseNote('Still closing on the runner. It will drop off the list once it’s done.')
        return
      }
      leave()
    } catch {
      setCloseNote('Couldn’t close this session')
    } finally {
      setClosing(false)
    }
  }, [sessionId, meta, navigate, onLeave])

  // --- "Disconnect from Slack": stop the session posting into its bound thread
  // and the thread's replies reaching it. Confirmed first — re-attaching means a
  // fresh share, which starts a new thread.
  const [detachingSlack, setDetachingSlack] = useState(false)
  const [slackNote, setSlackNote] = useState<string | null>(null)
  const detachSlack = useCallback(async () => {
    if (!sessionId || !meta?.slack) return
    if (!window.confirm(
      "Disconnect this session from its Slack thread? Replies there will no longer reach it, " +
      "and its replies will stop posting there.")) return
    setDetachingSlack(true)
    setSlackNote(null)
    try {
      await detachSessionFromSlack(sessionId)
      setMeta((prev) => (prev ? { ...prev, slack: null } : prev))
      setSlackNote('Disconnected from Slack')
    } catch (err) {
      setSlackNote(err instanceof Error ? err.message : 'Could not disconnect from Slack')
    } finally {
      setDetachingSlack(false)
    }
  }, [sessionId, meta?.slack, setMeta])

  // --- move. Where this session could be moved to — fetched only for a session
  // that is yours to move (`can_move`), since the menu item is the only reader.
  const [moveTargets, setMoveTargets] = useState<MoveTarget[] | undefined>(undefined)
  const canMove = Boolean(meta?.can_move)
  useEffect(() => {
    if (!canMove) return
    let live = true
    getSessionWorkspacePreference()
      .then((pref) => {
        if (live) setMoveTargets(pref.choices)
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [canMove])
  const moveToWorkspace = useCallback(async (workspace: string) => {
    if (!sessionId) return
    const moved = await moveSession(sessionId, workspace)
    setMeta((prev) => (prev ? { ...prev, workspace: moved.workspace } : prev))
    if (onLeave) return
    // The URL names the workspace; keep it true so a reload lands here.
    navigate(scopedPath(moved.workspace, `/sessions/${encodeURIComponent(sessionId)}`), {
      replace: true,
    })
  }, [sessionId, setMeta, onLeave, navigate])

  // --- notify on every completion: optimistic, rolled back on failure.
  const toggleNotify = useCallback(async () => {
    if (!sessionId || !meta) return
    const next = !meta.notify_every_completion
    setMeta((prev) => (prev ? { ...prev, notify_every_completion: next } : prev))
    try {
      await setSessionNotify(sessionId, next)
    } catch {
      setMeta((prev) => (prev ? { ...prev, notify_every_completion: !next } : prev))
    }
  }, [sessionId, meta, setMeta])

  return {
    canManage,
    renaming,
    setRenaming,
    renameTo,
    resetting,
    resetNote,
    resetFromTranscript,
    closing,
    closeNote,
    closeThisSession,
    detachingSlack,
    slackNote,
    detachSlack,
    canMove,
    moveTargets,
    moveToWorkspace,
    toggleNotify,
  }
}
