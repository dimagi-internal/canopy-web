import { useCallback, useEffect, useMemo, useState, type Dispatch, type SetStateAction } from 'react'
import type { PlacementRunner } from 'canopy-ui/chat'

import { ChatApiError, getSession, placeTurn, type ChatSessionDetail } from '@/api/chat'
import { listRunners, recentRunners, unpauseRunner, type RunnerOut } from '@/api/harness'
import {
  findBoundRunner,
  isBoundRunnerOffline,
  onlineSessionCapableRunners,
} from '@/components/chat/runnerEligibility'
import { withFreshLiveness } from '../chatPageLogic'

export const RECOVERY_POLL_MS = 30_000
/** How old a fleet read another page made may be and still open a chat (`recentRunners`). */
export const FLEET_REUSE_MS = 60_000

/** The offline-runner placement banner: whether the session's bound runner is
 *  offline or paused, where else the turn could go, and the actions (wait,
 *  continue on another runner, resume a paused one).
 *
 *  The session payload only carries `runner_name` (no runner id/liveness), so
 *  offline-ness is DERIVED by matching that name against the fleet-wide runner
 *  list, which is fetched once (or reused, when another page read it within
 *  FLEET_REUSE_MS) and re-polled while the banner shows. */
export function useRunnerPlacement(
  sessionId: string,
  meta: ChatSessionDetail | null,
  setMeta: Dispatch<SetStateAction<ChatSessionDetail | null>>,
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

  const boundOffline = isBoundRunnerOffline(meta?.runner_name, fleetRunners, meta?.runner_online)
  // Only runners the conversation's host allows (e.g. sensitive-data runners): the server refuses any
  // other placement, so offering one would only produce an error.
  const runnerRequirements = meta?.runner_requirements
  const continueOptions = useMemo(
    () => onlineSessionCapableRunners(fleetRunners, runnerRequirements),
    [fleetRunners, runnerRequirements],
  )
  // The bound runner's fleet row — the id `Resume` needs, and the `paused` /
  // `can_manage` flags that decide whether resuming is even on offer here.
  const boundRunner = findBoundRunner(meta?.runner_name, fleetRunners)
  const boundPaused = boundOffline && Boolean(boundRunner?.paused)
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

  // While the offline banner is up, a bound runner that recovers later in
  // the same page-load would otherwise leave a stale-offline false positive
  // forever. Poll on a modest interval for as long as the condition holds, so
  // recovery hides the banner; stop the instant it flips false (recovered, or
  // session switch).
  //
  // Each tick re-reads the SESSION's liveness too, not just the fleet: the
  // payload's `runner_online` is authoritative in `isBoundRunnerOffline`, so a
  // `false` read at mount outranks any fleet row that says the box is back.
  // Refreshing only the fleet is why a laptop that rebooted and reconnected
  // kept "<runner> is unavailable" up over a Connected, streaming chat until
  // the page was remounted (Jonathan, 2026-10-22).
  //
  // A tick also fires the moment the tab comes back to the foreground or the
  // network returns — a machine waking from sleep is exactly when the runner
  // reconnects, and background timers are throttled while it slept.
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
      Promise.allSettled([
        listRunners().then((r) => {
          if (!cancelled) setFleetRunners(r)
        }),
        getSession(sessionId).then((m) => {
          if (!cancelled) setMeta((prev) => withFreshLiveness(prev, m))
        }),
      ]).finally(() => {
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
  }, [boundOffline, sessionId, setMeta])

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
  }, [boundRunner, begin, placementFail, setMeta])

  const continueOn = useCallback(
    (runnerId: string) => {
      if (!runnerId) return
      begin()
      placeTurn(sessionId, runnerId)
        .then(() => {
          setPlaceInfo('Placed — the new runner will pick it up shortly.')
          refreshFleet()
        })
        .catch(placementFail)
        .finally(() => setPlacing(false))
    },
    [sessionId, begin, placementFail, refreshFleet],
  )

  return {
    fleetRunners,
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
