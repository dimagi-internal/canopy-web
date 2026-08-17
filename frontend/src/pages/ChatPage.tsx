import { useCallback, useEffect, useMemo, useState, useRef} from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ChatPanel,
  PlacementBanner,
  MenuPrompt,
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
  closeSession,
  placeTurn,
  answerMenu,
  ChatApiError,
  type ChatSessionDetail,
  uploadAttachment,
  deleteAttachment,
} from '@/api/chat'
import { closeIntent, closeResultMessage } from '@/components/chat/closeAction'
import { listRunners, unpauseRunner, type RunnerOut } from '@/api/harness'
import {
  findBoundRunner,
  isBoundRunnerOffline,
  onlineSessionCapableRunners,
} from '@/components/chat/runnerEligibility'
import {
  backfillAction,
  restToKitMessage,
  menuBlocksComposer,
  sendBlockReason,
  shouldShowLoadFull,
} from './chatPageLogic'

/** Render assistant/system message text through canopy's shared Markdown (the
 *  same renderer used across every AI-output surface — so it picks up remark-gfm
 *  AND remark-breaks, i.e. single newlines become line breaks instead of
 *  collapsing). Injected into the kit via its `renderMarkdown` seam so the kit
 *  itself stays free of react-markdown. */
function renderMarkdown(text: string) {
  return <Markdown className="text-sm leading-relaxed">{text}</Markdown>
}

/**
 * "Load full session" WAITS for the rows, it does not guess at them.
 *
 * It used to sleep a flat 1200 ms and read once. Measured on labs (2026-07-31)
 * the rows landed at t+14.6s, so the read happened 13 s early, returned the
 * identical tail, and the button reported success having changed nothing —
 * which is exactly what it looks like from a phone. Now it polls until the
 * transcript actually grows, which is also the only formulation that stays
 * correct as the runner gets faster: with eager persistence most sessions are
 * already complete server-side and the first poll returns immediately.
 */
const BACKFILL_POLL_MS = 600
const BACKFILL_TIMEOUT_MS = 30_000

/**
 * Standalone live-chat route (/w/:workspace/chat/:id). Wires canopy's
 * WebSocket URL + REST session-meta/history/liveness + react-markdown into the
 * reusable `canopy-ui/chat` ChatPanel. All live chat state lives in
 * `useSessionSocket`; this page supplies seams (attach/detach, scroll-back,
 * full backfill, running/idle) + a minimal title shell.
 */
export function ChatPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
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
  // The bound runner's fleet row — the id `Resume` needs, and the `paused` /
  // `can_manage` flags that decide whether resuming is even on offer here.
  const boundRunner = findBoundRunner(meta?.runner_name, fleetRunners)
  const boundPaused = boundOffline && Boolean(boundRunner?.paused)
  // The composer refuses rather than queueing — see sendBlockReason.
  const disabledReason = sendBlockReason({
    runnerName: meta?.runner_name,
    boundOffline,
    paused: boundPaused,
    blockedOnMenu: menuBlocksComposer(socket.state.menu),
  })
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

  // Un-park the bound runner from inside the chat. The fix for a pause is one
  // tap and it is YOUR decision being undone, so making you leave for
  // /supervisor to do it is what pushed people toward waiting instead.
  const resumeBoundRunner = useCallback(() => {
    if (!boundRunner) return
    setPlacing(true)
    setPlaceInfo(null)
    setPlaceError(null)
    unpauseRunner(boundRunner.id)
      .then((fresh) => {
        setFleetRunners((prev) => prev.map((r) => (r.id === fresh.id ? fresh : r)))
        // Drop the session payload's liveness snapshot — it was computed before
        // the unpause and is stale by construction — so the freshly-patched
        // fleet row answers instead. This clears a STALE READING, it does not
        // assert the box is up: unpause restores whatever the heartbeat says,
        // so a runner that is also offline keeps the banner, correctly.
        setMeta((prev) => (prev ? { ...prev, runner_online: null, runner_status: null } : prev))
        setPlaceInfo(`${fresh.name} resumed.`)
      })
      .catch(placementFail)
      .finally(() => setPlacing(false))
  }, [boundRunner, placementFail])

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

  const [closing, setClosing] = useState(false)
  const [closeNote, setCloseNote] = useState<string | null>(null)

  // Navigate away only when the session is REALLY gone (`closing: false`). When the
  // close was relayed to a runner the emdash task is still the truth, so stay put
  // and say so — bouncing to the list would claim a result we do not have yet.
  const closeThisSession = useCallback(async () => {
    if (!meta) return
    const intent = closeIntent(meta)
    if (intent.kind === 'blocked') {
      setCloseNote(intent.why)
      return
    }
    if (intent.confirm && !window.confirm('This chat is still working. Close it anyway?')) return
    setClosing(true)
    setCloseNote(null)
    try {
      const result = await closeSession(id)
      const message = closeResultMessage(result, meta)
      if (message) setCloseNote(message)
      else if (result.closing) setCloseNote('Closing on the runner…')
      else navigate(`/w/${meta.workspace}/chat`)
    } catch {
      setCloseNote('Couldn’t close this session')
    } finally {
      setClosing(false)
    }
  }, [id, meta, navigate])

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
      if (action === 'reload-after-delay') {
        // The runner is shipping. Poll the TAIL read for `backfill_pending` —
        // it carries the same flag for a nineteenth of the bytes (measured on
        // labs: 43 KB / 0.36s against 825 KB / 1.6-2.8s for ?full=true on a
        // 940-row session). Polling the full read instead meant ~11 downloads
        // of the entire transcript, on a phone, to answer a yes/no question;
        // it was slow enough to time out a 90s client. The flag is the exact
        // signal (cleared by the runner's final chunk), so waiting on it beats
        // both a fixed sleep and watching the row count grow — growth cannot
        // tell "still arriving" from "there was nothing more to send".
        const deadline = Date.now() + BACKFILL_TIMEOUT_MS
        while (Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, BACKFILL_POLL_MS))
          if (requestedId !== id) return
          const probe = await getSession(requestedId)
          if (requestedId !== id) return
          if (!probe.backfill_pending) break
        }
      }
      // Exactly ONE full read, after the wait — not one per poll. Whatever has
      // landed by the deadline is applied either way: a partially rebuilt
      // history beats discarding it and showing the tail.
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
    runnerName: meta?.runner_name,
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

  // Answering the dialog a blocked agent is waiting on. The optimistic clear is
  // deliberate: the runner re-reads the screen and refuses a stale answer, so the
  // worst case is a menu that reappears — better than buttons that stay live
  // against a dialog that is already gone.
  const [answering, setAnswering] = useState(false)
  const [answerError, setAnswerError] = useState('')
  const answeredAt = useRef(0)
  const onAnswerMenu = useCallback(
    async (option: number | null, selections?: number[][] | null,
           texts?: (string | null)[] | null) => {
      if (!id) return
      setAnswering(true)
      setAnswerError('')
      // Remember what we were looking at: if the menu DISAPPEARS right after this
      // tap, the runner reconciled it away — either the key landed, or the screen
      // said the dialog was already gone. A cleared menu carries no note (there
      // is no object left to hang one on), so the transient explanation belongs
      // here, where we know a tap just happened.
      answeredAt.current = Date.now()
      try {
        const res = await answerMenu(id, option, selections, texts)
        if (!res.ok) {
          setAnswerError(
            res.reason === 'unavailable'
              ? 'That runner is offline — answer it in emdash.'
              : res.reason === 'unbound'
                ? 'This session has no runner to answer on.'
                : 'Could not answer.',
          )
        }
      } catch {
        setAnswerError('Could not answer.')
      } finally {
        setAnswering(false)
      }
    },
    [id],
  )

  // The reconciliation rule, client side: the runner clears a menu both when the
  // key LANDED and when the screen turned out to have no dialog. Neither leaves
  // a note on the server (the object is gone), and neither needs one — except in
  // the second case, where nothing visibly happened and silence would read as
  // the button failing again. We know a tap just happened, so we say it here.
  const [vanishedNote, setVanishedNote] = useState('')
  const hadMenu = useRef(false)
  useEffect(() => {
    const has = Boolean(socket.state.menu)
    if (hadMenu.current && !has && Date.now() - answeredAt.current < 30_000) {
      setVanishedNote('That dialog is gone — either your answer landed, or it had already been answered.')
    }
    if (has) setVanishedNote('')
    hadMenu.current = has
  }, [socket.state.menu])

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
  // The window between pressing send and the agent actually starting: the turn
  // is enqueued, a runner has to claim it and (on a laptop) drive it into
  // emdash before Claude sees a prompt at all. NOTHING could report during it —
  // `activity` waits on a UserPromptSubmit hook that cannot fire until the
  // prompt lands, and `running` comes from the session report, which lags a
  // cycle. So a phone send showed no feedback for seconds and then jumped
  // straight to running, which reads as the app having ignored you.
  //
  // `awaitingReply` is set the instant you press send, client-side with no
  // round trip, so it is the only thing that can answer immediately. Shown as
  // "queued" rather than folded into "running" because the distinction is the
  // useful part: queued means canopy has not started your turn yet, running
  // means the agent is thinking. Confusing the two hides where the delay is.
  const liveQueued = socket.awaitingReply;
  // `meta` is fetched ONCE per session id and never refreshed, and `running`
  // derives from a 120s window on the runner's last report — so this is a
  // PAGE-LOAD SNAPSHOT that can only decay. It used to outrank everything below
  // `blocked`, which quietly cost #490 its whole point: open a chat you were
  // just using and the header already said "running", so pressing send changed
  // nothing on screen and the "queued" branch was unreachable. Demoted to the
  // last resort — consulted only while nothing live has spoken, and dropped for
  // good once anything has.
  const staleRunning = liveWorking === undefined && Boolean(meta?.running);
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
        ) : liveWorking ? (
          <span className="flex shrink-0 items-center gap-1 text-[12px] font-medium text-success">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
            running{meta?.runner_name ? ` · ${meta.runner_name}` : ''}
          </span>
        ) : liveQueued ? (
          <span className="flex shrink-0 items-center gap-1 text-[12px] font-medium text-muted-foreground">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-muted-foreground" />
            queued{meta?.runner_name ? ` · ${meta.runner_name}` : ''}
          </span>
        ) : staleRunning ? (
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
          {closeNote && <span className="text-[12px] text-muted-foreground">{closeNote}</span>}
          <button
            type="button"
            onClick={() => void closeThisSession()}
            disabled={closing}
            title="Close this session — deletes its emdash task. The transcript is kept."
            className="rounded-md border border-border bg-card px-2 py-1 text-[12px] text-foreground-secondary hover:bg-muted disabled:opacity-50"
          >
            {closing ? 'Closing…' : 'Close session'}
          </button>
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
          // Keep a half-typed message across a route change or a closed tab.
          // Nothing else holds it: alone in a session the body is never
          // mirrored to the server until the moment you send.
          draftPersistKey={id}
          emptyState={emptyState}
          historySlot={historySlot}
          // Refuse the send outright rather than queueing it at a box that
          // cannot answer — the banner above holds the ways out.
          disabledReason={disabledReason}
          banner={
            socket.state.menu ? (
              <MenuPrompt
                menu={socket.state.menu}
                busy={answering}
                error={answerError}
                onAnswer={onAnswerMenu}
              />
            ) : vanishedNote ? (
              <div className="rounded-md border border-border bg-muted/40 px-3 py-2 text-[12px] text-muted-foreground">
                {vanishedNote}
              </div>
            ) : boundOffline ? (
              <PlacementBanner
                runnerName={meta?.runner_name ?? ''}
                eligibleRunners={placementRunners}
                busy={placing}
                error={placeError}
                info={placeInfo}
                onWait={waitForIt}
                onPlace={continueOn}
                paused={boundPaused}
                pausedNote={boundRunner?.paused_note || undefined}
                // Pause/resume is pairer-only server-side; `can_manage` is the
                // row saying so. Without it, Resume would 404 and read as the
                // runner refusing to come back.
                onResume={boundPaused && boundRunner?.can_manage ? resumeBoundRunner : undefined}
              />
            ) : undefined
          }
        />
      </div>
    </div>
  )
}

export default ChatPage
