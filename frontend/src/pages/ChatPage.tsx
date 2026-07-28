import { useCallback, useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import {
  ChatPanel,
  PlacementBanner,
  useSessionSocket,
  type PendingAttachment,
  type PlacementRunner,
} from 'canopy-ui/chat'
import { Markdown } from '@/components/Markdown'
import { wsUrl } from '@/lib/wsUrl'
import {
  getSession,
  listMessages,
  attachSession,
  detachSession,
  requestBackfill,
  resetSession,
  placeTurn,
  ChatApiError,
  type ChatSessionDetail,
  uploadAttachment,
  deleteAttachment,
} from '@/api/chat'
import { listRunners, type RunnerOut } from '@/api/harness'
import { isBoundRunnerOffline, onlineSessionCapableRunners } from '@/components/chat/runnerEligibility'
import { backfillAction, restToKitMessage, shouldShowLoadFull } from './chatPageLogic'

/** Render assistant/system message text through canopy's shared Markdown (the
 *  same renderer used across every AI-output surface — so it picks up remark-gfm
 *  AND remark-breaks, i.e. single newlines become line breaks instead of
 *  collapsing). Injected into the kit via its `renderMarkdown` seam so the kit
 *  itself stays free of react-markdown. */
function renderMarkdown(text: string) {
  return <Markdown className="text-sm leading-relaxed">{text}</Markdown>
}

const BACKFILL_SETTLE_DELAY_MS = 1200

/**
 * Standalone live-chat route (/w/:workspace/chat/:id). Wires canopy's
 * WebSocket URL + REST session-meta/history/liveness + react-markdown into the
 * reusable `canopy-ui/chat` ChatPanel. All live chat state lives in
 * `useSessionSocket`; this page supplies seams (attach/detach, scroll-back,
 * full backfill, running/idle) + a minimal title shell.
 */
export function ChatPage() {
  const { id = '' } = useParams()
  const [meta, setMeta] = useState<ChatSessionDetail | null>(null)
  const [metaError, setMetaError] = useState<string | null>(null)
  // Scroll-back cursor, seeded from the REST detail (the WS `session.state`
  // snapshot doesn't carry it — it's frozen to the tail page it was built from).
  const [hasMoreBefore, setHasMoreBefore] = useState(false)
  const [oldestTurn, setOldestTurn] = useState<number | null>(null)
  const [loadingEarlier, setLoadingEarlier] = useState(false)
  const [loadingFull, setLoadingFull] = useState(false)
  const [resetting, setResetting] = useState(false)
  const [resetNote, setResetNote] = useState('')
  const [historyUnavailable, setHistoryUnavailable] = useState(false)

  // Offline-runner placement banner state. The session payload only carries
  // `runner_name` (no runner id/liveness), so offline-ness is DERIVED by
  // matching that name against the fleet-wide runner list.
  const [fleetRunners, setFleetRunners] = useState<RunnerOut[]>([])
  const [placing, setPlacing] = useState(false)
  // Staged attachments. Uploaded eagerly on pick/paste/drop so the file is on
  // the server before you press send — sending then sweeps up whatever the
  // session is holding, which is why no ids need to cross the WebSocket.
  const [attachments, setAttachments] = useState<PendingAttachment[]>([])

  const handleAttach = useCallback(
    (files: File[]) => {
      for (const file of files) {
        // Optimistic chip keyed on a temp id, swapped for the server's id on
        // success. Without it a large paste looks like nothing happened.
        const tempId = `pending:${file.name}:${Date.now()}:${Math.random()}`
        setAttachments((prev) => [...prev, { id: tempId, filename: file.name, uploading: true }])
        uploadAttachment(id!, file)
          .then((saved) =>
            setAttachments((prev) =>
              prev.map((a) => (a.id === tempId ? { id: saved.id, filename: saved.filename } : a)),
            ),
          )
          .catch((err: unknown) =>
            setAttachments((prev) =>
              prev.map((a) =>
                a.id === tempId
                  ? { ...a, uploading: false, error: err instanceof Error ? err.message : 'failed' }
                  : a,
              ),
            ),
          )
      }
    },
    [id],
  )

  const handleRemoveAttachment = useCallback((attachmentId: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== attachmentId))
    // A failed upload has no server row (its id is still the temp one), so only
    // ask the server to forget the ones it actually knows about.
    if (!attachmentId.startsWith('pending:')) {
      deleteAttachment(attachmentId).catch(() => {
        /* the chip is already gone from the UI; a stray row is swept server-side */
      })
    }
  }, [])
  const [placeInfo, setPlaceInfo] = useState<string | null>(null)
  const [placeError, setPlaceError] = useState<string | null>(null)

  const socket = useSessionSocket({ sessionId: id, wsUrl })

  // Session meta + scroll-back cursor seed.
  useEffect(() => {
    if (!id) return
    setMeta(null)
    setMetaError(null)
    setHistoryUnavailable(false)
    getSession(id)
      .then((m) => {
        setMeta(m)
        setHasMoreBefore(m.has_more_before)
        setOldestTurn(m.oldest_loaded_turn_index ?? null)
      })
      .catch((err: unknown) => {
        setMetaError(err instanceof Error ? err.message : 'session not found')
      })
  }, [id])

  // Reset the placement-banner UI state on session switch, so a stale info/
  // error message doesn't leak across sessions. The "Continue on…" picker's
  // open/closed state is now local to <PlacementBanner> (a fresh mount per
  // session key anyway, per router.tsx's `key={id}`), so it needs no reset here.
  useEffect(() => {
    setPlacing(false)
    setPlaceInfo(null)
    setPlaceError(null)
  }, [id])

  // Fleet runner list, for deriving the bound runner's liveness + the
  // "Continue on…" eligible set. Fetched once on mount; re-polled below
  // while the offline banner is showing (see the recovery-poll effect).
  useEffect(() => {
    let live = true
    listRunners()
      .then((r) => {
        if (live) setFleetRunners(r)
      })
      .catch(() => {
        /* non-fatal: the offline banner just won't have evidence to show */
      })
    return () => {
      live = false
    }
  }, [])

  const boundOffline = isBoundRunnerOffline(
    meta?.runner_name,
    fleetRunners,
    meta?.runner_online,
  )
  const continueOptions = useMemo(
    () => onlineSessionCapableRunners(fleetRunners),
    [fleetRunners],
  )
  // <PlacementBanner>'s eligible-runner shape, mapped from the fleet-derived
  // (already online + session-capable) options above.
  const placementRunners: PlacementRunner[] = useMemo(
    () => continueOptions.map((r) => ({ id: r.id, name: r.name, online: r.status === 'online' })),
    [continueOptions],
  )

  // Best-effort fleet resync, used both by the recovery poll below and after
  // a successful placement — non-fatal on failure (the banner just keeps
  // showing the last-known snapshot).
  const refreshFleet = useCallback(() => {
    listRunners()
      .then((r) => setFleetRunners(r))
      .catch(() => {
        /* non-fatal: keep the last-known fleet snapshot */
      })
  }, [])

  // While the offline banner is up, a bound runner that recovers later in
  // the same page-load would otherwise leave a stale-offline false positive
  // forever — the fleet list is only fetched once on mount. Poll on a modest
  // interval for as long as the condition holds, so recovery hides the
  // banner; stop the instant it flips false (recovered, or session switch).
  useEffect(() => {
    if (!boundOffline) return
    let cancelled = false
    const POLL_MS = 30_000
    let timer: ReturnType<typeof setTimeout>
    const tick = () => {
      listRunners()
        .then((r) => {
          if (!cancelled) setFleetRunners(r)
        })
        .catch(() => {
          /* non-fatal: retry next tick */
        })
        .finally(() => {
          if (!cancelled) timer = setTimeout(tick, POLL_MS)
        })
    }
    timer = setTimeout(tick, POLL_MS)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [boundOffline])

  const placementFail = useCallback((err: unknown) => {
    if (err instanceof ChatApiError && err.status === 404) {
      setPlaceInfo('No pending message to place.')
      return
    }
    if (err instanceof ChatApiError && err.status === 422) {
      setPlaceError(err.message)
      return
    }
    setPlaceError(err instanceof Error ? err.message : 'Could not place the turn.')
  }, [])

  const waitForIt = useCallback(() => {
    setPlacing(true)
    setPlaceInfo(null)
    setPlaceError(null)
    placeTurn(id, 'wait')
      .then(() => {
        setPlaceInfo('Waiting for the bound runner.')
        refreshFleet()
      })
      .catch(placementFail)
      .finally(() => setPlacing(false))
  }, [id, placementFail, refreshFleet])

  const continueOn = useCallback(
    (runnerId: string) => {
      if (!runnerId) return
      setPlacing(true)
      setPlaceInfo(null)
      setPlaceError(null)
      placeTurn(id, runnerId)
        .then(() => {
          setPlaceInfo('Placed — the new runner will pick it up shortly.')
          refreshFleet()
        })
        .catch(placementFail)
        .finally(() => setPlacing(false))
    },
    [id, placementFail, refreshFleet],
  )

  // Attach-on-open / detach-on-unmount. The server's attach counter floors at
  // 0 rather than going negative, so a detach that lands BEFORE its paired
  // attach is silently absorbed — the attach then still increments, leaving
  // the count stuck net +1 and a runner-bound session's `stream_desired`
  // never clears. These are fire-and-forget HTTP calls with no other
  // ordering guarantee, so we chain the detach off the attach promise
  // (`.finally`, not `.then`/`.catch`, so a failed attach still detaches):
  // the detach request is never even issued until the attach one has
  // settled, which makes the wrong-order race structurally impossible.
  // React StrictMode's mount/unmount/remount double-invoke is fine here —
  // each attach/detach pair is still strictly ordered within itself.
  useEffect(() => {
    if (!id) return
    const attached = attachSession(id).catch(() => {
      /* non-fatal: a failed attach just means no bound runner to notify */
    })
    return () => {
      void attached.finally(() => {
        void detachSession(id).catch(() => {
          /* non-fatal */
        })
      })
    }
  }, [id])

  const loadEarlier = useCallback(async () => {
    if (oldestTurn == null || loadingEarlier) return
    // Capture the session this call was made for. Belt-and-suspenders on top
    // of the route-level `key={id}` remount (router.tsx): even if this
    // component instance somehow outlives the session it was fetching for,
    // a stale response can never splice into whatever session is current.
    const requestedId = id
    setLoadingEarlier(true)
    try {
      const page = await listMessages(requestedId, oldestTurn)
      if (requestedId !== id) return
      if (page.messages.length > 0) {
        socket.prependMessages(page.messages.map(restToKitMessage))
        setOldestTurn(page.messages[0].turn_index)
      }
      // A local (origin=runner) session with no server rows yet returns an
      // empty page with has_more_before=false — history lives on the runner,
      // so fall through to offering the full backfill instead.
      setHasMoreBefore(page.messages.length > 0 ? page.has_more_before : false)
    } catch {
      /* keep what's shown; the button stays available to retry */
    } finally {
      if (requestedId === id) setLoadingEarlier(false)
    }
  }, [id, oldestTurn, loadingEarlier, socket])

  /** Drop canopy's derived copy and rebuild it from the runner's transcript.
   * Cheap and repeatable: the rows are a cache of a file on the runner's disk,
   * so this is the way to pick up anything that happened outside a turn. */
  const resetFromTranscript = useCallback(async () => {
    if (!id) return
    setResetting(true)
    setResetNote('')
    try {
      const res = await resetSession(id)
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
  }, [id])

  const loadFull = useCallback(async () => {
    if (loadingFull) return
    // See loadEarlier: capture the session this call was made for so a
    // stale response can never apply to a different session.
    const requestedId = id
    setHistoryUnavailable(false)
    setLoadingFull(true)
    try {
      const res = await requestBackfill(requestedId)
      if (requestedId !== id) return
      const action = backfillAction(res.status)
      if (action === 'unavailable') {
        setHistoryUnavailable(true)
        return
      }
      // reload-now = already server-full; reload-after-delay = the runner is
      // shipping it — give it a beat to land before pulling the full session.
      if (action === 'reload-after-delay') {
        await new Promise((r) => setTimeout(r, BACKFILL_SETTLE_DELAY_MS))
      }
      const full = await getSession(requestedId, { full: true })
      if (requestedId !== id) return
      socket.prependMessages(full.messages.map(restToKitMessage))
      setHasMoreBefore(false)
      setOldestTurn(full.oldest_loaded_turn_index ?? null)
    } catch {
      if (requestedId === id) setHistoryUnavailable(true)
    } finally {
      if (requestedId === id) setLoadingFull(false)
    }
  }, [id, loadingFull, socket])

  const emptyState = useMemo(
    () => (
      <div className="flex h-full flex-col items-center justify-center gap-1 p-8 text-center text-sm text-muted-foreground">
        <div className="text-foreground">Start the conversation</div>
        <div className="text-xs">Type a message below to begin.</div>
      </div>
    ),
    [],
  )

  const showLoadFull = shouldShowLoadFull({
    origin: meta?.origin,
    hasMoreBefore,
    historyUnavailable,
  })

  // The slot is PINNED above the transcript, so render nothing at all when
  // there's nothing to offer — otherwise every session carries an empty strip.
  const hasHistoryControls = historyUnavailable || hasMoreBefore || showLoadFull

  const historySlot = !hasHistoryControls ? undefined : (
    <div className="flex flex-col items-center gap-1 border-b border-border py-2">
      {historyUnavailable && (
        <p className="rounded-md border border-warning/30 bg-warning/10 px-3 py-1.5 text-[12px] text-warning">
          Full history unavailable — runner offline. Showing the latest messages.
        </p>
      )}
      {hasMoreBefore && (
        <button
          type="button"
          onClick={() => void loadEarlier()}
          disabled={loadingEarlier}
          className="rounded-md border border-border bg-card px-3 py-1 text-[12px] text-foreground-secondary hover:bg-muted disabled:opacity-50"
        >
          {loadingEarlier ? 'Loading…' : 'Load earlier'}
        </button>
      )}
      {showLoadFull && (
        <button
          type="button"
          onClick={() => void loadFull()}
          disabled={loadingFull}
          className="rounded-md border border-border bg-card px-3 py-1 text-[12px] text-foreground-secondary hover:bg-muted disabled:opacity-50"
        >
          {loadingFull ? 'Loading…' : 'Load full session'}
        </button>
      )}
    </div>
  )

  // undefined = no hook has reported for this session yet, so defer to the
  // server's coarser flag rather than asserting idle.
  const liveWorking =
    socket.state.activity === undefined
      ? undefined
      : socket.state.activity === "working";
  // The agent asked for a human and stopped — a permission prompt, or an idle
  // wait for input. It previously rendered as "running", which is the worst way
  // to get this wrong: you wait on an agent that is waiting on you.
  const liveBlocked = socket.state.activity === "blocked";
  const title = meta?.title?.trim() || 'Chat'

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-border bg-background px-4 py-2">
        <h1 className="truncate text-sm font-semibold text-foreground">{title}</h1>
        {/* Live agent activity beats the server's liveness fields when a hook has
            reported. `meta.running` derives from the runner's session report —
            emdash's own last_interacted_at, on a report cycle with a 120s window
            — so it could not show "working" during the part of a turn where
            Claude is thinking and nothing has been output yet. The hook fires the
            instant the turn starts. Falls back to `meta.running` when no hook has
            spoken (an older runner, or forwarding off). */}
        {liveBlocked ? (
          <span className="flex shrink-0 items-center gap-1 text-[12px] font-medium text-warning">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-warning" />
            needs you{meta?.runner_name ? ` · ${meta.runner_name}` : ''}
          </span>
        ) : (liveWorking ?? meta?.running) ? (
          <span className="flex shrink-0 items-center gap-1 text-[12px] font-medium text-success">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
            running{meta?.runner_name ? ` · ${meta.runner_name}` : ''}
          </span>
        ) : meta?.runner_name ? (
          <span className="shrink-0 text-[12px] text-muted-foreground">
            idle · {meta.runner_name}
          </span>
        ) : null}
        {metaError && <span className="text-xs text-muted-foreground">· {metaError}</span>}
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {resetNote && <span className="text-[12px] text-muted-foreground">{resetNote}</span>}
          <button
            type="button"
            onClick={() => void resetFromTranscript()}
            disabled={resetting}
            title="Drop canopy's copy and rebuild this conversation from the runner's transcript"
            className="rounded-md border border-border bg-card px-2 py-1 text-[12px] text-foreground-secondary hover:bg-muted disabled:opacity-50"
          >
            {resetting ? 'Resetting…' : 'Reset from transcript'}
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1">
        <ChatPanel
          state={socket.state}
          connected={socket.connected}
          currentUserId={socket.state.current_user_id}
          onSend={() => {
            // The server consumes whatever the session is holding, so the chips
            // are spent the moment we send — leaving them would imply they will
            // ride along again on the next message.
            setAttachments([])
            socket.sendChat()
          }}
          onStop={socket.stopChat}
          awaitingReply={socket.awaitingReply}
          attachments={attachments}
          onAttach={handleAttach}
          onRemoveAttachment={handleRemoveAttachment}
          onUpdateDraft={socket.updateDraft}
          onTakeOver={socket.takeOverDraft}
          onDiscard={socket.discardDraft}
          renderMarkdown={renderMarkdown}
          emptyState={emptyState}
          historySlot={historySlot}
          banner={
            boundOffline ? (
              <PlacementBanner
                runnerName={meta?.runner_name ?? ''}
                eligibleRunners={placementRunners}
                busy={placing}
                error={placeError}
                info={placeInfo}
                onWait={waitForIt}
                onPlace={continueOn}
              />
            ) : undefined
          }
        />
      </div>
    </div>
  )
}

export default ChatPage
