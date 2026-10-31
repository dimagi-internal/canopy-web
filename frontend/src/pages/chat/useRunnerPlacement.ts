import { useCallback, useEffect, useMemo, useState, type Dispatch, type SetStateAction } from 'react'
import type { PlacementRunner, TurnStatus } from 'canopy-ui/chat'

import { ChatApiError, getSession, placeTurn, transferSession, type ChatSessionDetail } from '@/api/chat'
import { listRunners, recentRunners, unpauseRunner, type RunnerOut } from '@/api/harness'
import { findBoundRunner, onlineSessionCapableRunners } from '@/components/chat/runnerEligibility'
import { withFreshLiveness } from '../chatPageLogic'

export const RECOVERY_POLL_MS = 30_000
/** How old a fleet read another page made may be and still open a chat (`recentRunners`). */
export const FLEET_REUSE_MS = 60_000

/** Whether a turn is held by a runner that is away: queued for an offline or
 *  paused box (`waiting_runner`; `parked` when someone paused it), or claimed by
 *  one that went offline mid-turn (`paused`). The server's answer, live on the
 *  socket — nothing here guesses from a runner list. */
export function heldByAbsentRunner(status: TurnStatus | null | undefined): boolean {
  return status?.state === 'waiting_runner' || status?.state === 'paused'
}

/** The offline-runner placement banner: whether the turn is held by a runner
 *  that is away (from the server's `turn_status`, `heldByAbsentRunner`), where
 *  else the turn could go, and the actions (wait, continue on another runner,
 *  resume a paused one).
 *
 *  The fleet list is read only for the choices — which runners "Continue on…"
 *  offers, and the paused runner's id for Resume. It is fetched once (or
 *  reused, when another page read it within FLEET_REUSE_MS) and re-polled
 *  while the banner shows. */
export function useRunnerPlacement(
  sessionId: string,
  meta: ChatSessionDetail | null,
  setMeta: Dispatch<SetStateAction<ChatSessionDetail | null>>,
  turnStatus?: TurnStatus | null,
) {
  const [fleetRunners, setFleetRunners] = useState<RunnerOut[]>(() => recentRunners(FLEET_REUSE_MS) ?? [])
  const [placing, setPlacing] = useState(false)
  const [placeInfo, setPlaceInfo] = useState<string | null>(null)
  const [placeError, setPlaceError] = useState<string | null>(null)

  // Reset the banner UI state on session switch, so a stale info/error message
  // doesn't leak across sessions. The "Continue on…" picker's open/closed state
  // is local to <PlacementBanner> (a fresh mount per session key anyway, per
  // router.tsx's `key={id}`), so it needs no reset here.
  useEffect(() => {
    setPlacing(false)
    setPlaceInfo(null)
    setPlaceError(null)
  }, [sessionId])

  useEffect(() => {
    if (recentRunners(FLEET_REUSE_MS)) return
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

  const boundOffline = heldByAbsentRunner(turnStatus)
  // The box the turn waits on: the one holding it, else the one it is queued
  // for, else the session's binding.
  const runnerName = turnStatus?.claimed_by ?? turnStatus?.runners?.[0] ?? meta?.runner_name ?? null
  // A Resume tapped here: the pause is lifted, whatever the status says until
  // the server's next one arrives.
  const [resumedFor, setResumedFor] = useState<TurnStatus | null | undefined>(undefined)
  const resumed = resumedFor !== undefined && resumedFor === turnStatus
  // Only runners the conversation's host allows (e.g. sensitive-data runners): the server refuses any
  // other placement, so offering one would only produce an error.
  const runnerRequirements = meta?.runner_requirements
  const continueOptions = useMemo(
    () => onlineSessionCapableRunners(fleetRunners, runnerRequirements),
    [fleetRunners, runnerRequirements],
  )
  // The bound runner's fleet row — the id `Resume` needs, and the `paused` /
  // `can_manage` flags that decide whether resuming is even on offer here.
  const boundRunner = findBoundRunner(runnerName, fleetRunners)
  const boundPaused =
    turnStatus?.state === 'waiting_runner' && Boolean(turnStatus.parked) && !resumed
  // <PlacementBanner>'s eligible-runner shape, mapped from the fleet-derived
  // (already online + session-capable) options above.
  const placementRunners: PlacementRunner[] = useMemo(
    () => continueOptions.map((r) => ({ id: r.id, name: r.name, online: r.status === 'online' })),
    [continueOptions],
  )

  // Best-effort fleet resync after a successful placement — non-fatal on
  // failure (the banner just keeps showing the last-known snapshot).
  const refreshFleet = useCallback(() => {
    listRunners()
      .then((r) => setFleetRunners(r))
      .catch(() => {
        /* non-fatal: keep the last-known fleet snapshot */
      })
  }, [])

  // While the banner is up, keep the choices fresh: a runner that comes online
  // is one more place to continue on. A tick also fires the moment the tab
  // comes back to the foreground or the network returns, since background
  // timers are throttled while a machine sleeps. Whether the banner shows at
  // all follows the turn status on the socket, not this poll.
  useEffect(() => {
    if (!boundOffline) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let inFlight = false
    const tick = () => {
      // One read at a time, so a wake event landing mid-poll cannot start a
      // second timer chain.
      if (inFlight) return
      inFlight = true
      clearTimeout(timer)
      listRunners()
        .then((r) => {
          if (!cancelled) setFleetRunners(r)
        })
        .catch(() => {
          /* non-fatal: keep the last-known choices */
        })
        .finally(() => {
          inFlight = false
          if (!cancelled) timer = setTimeout(tick, RECOVERY_POLL_MS)
        })
    }
    const onVisible = () => {
      if (document.visibilityState === 'visible') tick()
    }
    timer = setTimeout(tick, RECOVERY_POLL_MS)
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('online', tick)
    return () => {
      cancelled = true
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('online', tick)
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

  const begin = useCallback(() => {
    setPlacing(true)
    setPlaceInfo(null)
    setPlaceError(null)
  }, [])

  const waitForIt = useCallback(() => {
    begin()
    placeTurn(sessionId, 'wait')
      .then(() => {
        setPlaceInfo('Waiting for the bound runner.')
        refreshFleet()
      })
      .catch(placementFail)
      .finally(() => setPlacing(false))
  }, [sessionId, begin, placementFail, refreshFleet])

  // Un-park the bound runner from inside the chat. The fix for a pause is one
  // tap and it is YOUR decision being undone, so making you leave for
  // /supervisor to do it is what pushed people toward waiting instead.
  const resumeBoundRunner = useCallback(() => {
    if (!boundRunner) return
    begin()
    unpauseRunner(boundRunner.id)
      .then((fresh) => {
        setFleetRunners((prev) => prev.map((r) => (r.id === fresh.id ? fresh : r)))
        // The pause is lifted; the banner stops saying so at once. Whether the
        // box is also up is the server's next turn status to say.
        setResumedFor(turnStatus)
        setPlaceInfo(`${fresh.name} resumed.`)
      })
      .catch(placementFail)
      .finally(() => setPlacing(false))
  }, [boundRunner, begin, placementFail, turnStatus])

  // With a message waiting, re-pin it. With none, `place` has nothing to move
  // and 404s, so move the whole session instead (`transfer`), then re-read it
  // so the banner and composer follow it to the new runner.
  const transferTo = useCallback(
    (runnerId: string) =>
      transferSession(sessionId, runnerId).then((res) => {
        if (res.status === 'pending') {
          const who = res.approvers?.length ? ` — waiting for ${res.approvers.join(', ')} to approve` : ''
          setPlaceInfo(`Asked to move this session${who}.`)
          return
        }
        setPlaceInfo(`Moved to ${res.runner}.`)
        refreshFleet()
        return getSession(sessionId).then((m) => setMeta((prev) => withFreshLiveness(prev, m)))
      }),
    [sessionId, refreshFleet, setMeta],
  )

  const continueOn = useCallback(
    (runnerId: string) => {
      if (!runnerId) return
      begin()
      placeTurn(sessionId, runnerId)
        .then(() => {
          setPlaceInfo('Placed — the new runner will pick it up shortly.')
          refreshFleet()
        })
        .catch((err) => {
          if (err instanceof ChatApiError && err.status === 404) return transferTo(runnerId)
          throw err
        })
        .catch(placementFail)
        .finally(() => setPlacing(false))
    },
    [sessionId, begin, placementFail, refreshFleet, transferTo],
  )

  return {
    fleetRunners,
    runnerName,
    boundOffline,
    boundRunner,
    boundPaused,
    placementRunners,
    placing,
    placeInfo,
    placeError,
    waitForIt,
    resumeBoundRunner,
    continueOn,
  }
}
