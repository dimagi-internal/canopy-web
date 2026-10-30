import type { JSX } from 'react'
import { holdFleet, releaseFleetHold, stopFleet, type FleetHoldOut } from '@/api/harness'
import { HoldBanner, HoldStopPanel } from './HoldStopPanel'

// The SYSTEM controls (apps/harness/models.py::FleetHold, /fleet-stop): the hold and
// stop-everything for every workspace at once. For SYSTEM superusers
// (`User.is_superuser`); the per-workspace pair is WorkspaceControlsCard.
//
// A named holder such as Ada may switch the hold ON but not off, and gets no stop —
// an agent can pause the fleet, only a person restarts it or cuts work off. Everyone
// else sees only the banner while it is on, so a member whose turn sits queued can
// see why. Nothing at all when it is off. The page owns the polling.
export function FleetHoldCard({
  hold,
  onChange,
}: {
  hold: FleetHoldOut | null
  onChange: (hold: FleetHoldOut) => void
}): JSX.Element | null {
  if (!hold || (!hold.held && !hold.can_hold)) return null

  const banner = hold.held && (
    <HoldBanner
      headline="Fleet on hold — no runner starts anything"
      note={hold.note}
      heldByEmail={hold.held_by_email}
      heldAt={hold.held_at}
      queued={hold.queued}
      testid="fleet-hold-banner"
    />
  )
  if (!hold.can_hold) return banner || null

  return (
    <div className="flex flex-col gap-2">
      {banner}
      <HoldStopPanel
        title="System controls · system superusers"
        scope="every workspace"
        held={hold.held}
        mayToggle={hold.held ? hold.can_release : true}
        heldHelp={
          hold.can_release
            ? 'On. Nothing new starts, in any workspace. Turn off to let the queue run.'
            : 'On. Only a system superuser can turn it off.'
        }
        onHold={(note) => holdFleet(note).then(onChange)}
        onRelease={() => releaseFleetHold().then(onChange)}
        canStop={hold.can_stop_running}
        onStop={stopFleet}
        testid="fleet-hold"
      />
    </div>
  )
}
