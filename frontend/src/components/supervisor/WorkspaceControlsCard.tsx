import type { JSX } from 'react'
import {
  getWorkspaceHold,
  holdWorkspace,
  releaseWorkspaceHold,
  stopWorkspace,
  type WorkspaceHoldOut,
} from '@/api/harness'
import { usePolled } from '@/hooks/usePolled'
import { HoldBanner, HoldStopPanel } from './HoldStopPanel'

// The WORKSPACE controls (Workspace.held, /workspaces/{slug}/stop): the same hold and
// stop-everything as the system's, for one workspace and every workspace below it.
// For its owners and SUPERUSERS (`control`, apps/workspaces/permissions.py) — a
// workspace superuser has an owner's keys as oversight, without the owner's email.
// Every member sees the banner while it is on; an ancestor's hold shows here too,
// and is released on that workspace.
export function WorkspaceControlsCard({
  slug,
  name,
}: {
  slug: string | null
  name: string
}): JSX.Element | null {
  // A failed read hides the card (as it always has); `usePolled` keeps the last
  // good value on error, so the catch maps it to null here instead.
  const { data: hold = null, setData: setHold } = usePolled(
    (): Promise<WorkspaceHoldOut | null> => getWorkspaceHold(slug as string).catch(() => null),
    { ms: 30_000, deps: [slug], enabled: Boolean(slug) },
  )

  if (!slug || !hold || hold.workspace !== slug || (!hold.held && !hold.can_control)) return null

  const inherited = hold.held_by_workspace
  const banner = hold.held && (
    <HoldBanner
      headline={`${inherited ? inherited : name} on hold — nothing starts here`}
      note={hold.note}
      heldByEmail={hold.held_by_email}
      heldAt={hold.held_at}
      queued={hold.queued}
      testid="workspace-hold-banner"
    />
  )
  if (!hold.can_control) return banner || null

  return (
    <div className="flex flex-col gap-2">
      {banner}
      <HoldStopPanel
        title={`Workspace controls · ${name} · owners and superusers`}
        scope={`${name} and every workspace below it`}
        held={hold.held}
        mayToggle={!inherited}
        heldHelp={
          inherited
            ? `On, from ${inherited} above this workspace — turn it off there.`
            : `On. Nothing new starts in ${name} or below. Turn off to let the queue run.`
        }
        onHold={(note) => holdWorkspace(slug, note).then(setHold)}
        onRelease={() => releaseWorkspaceHold(slug).then(setHold)}
        canStop
        onStop={() => stopWorkspace(slug)}
        testid="workspace-controls"
      />
    </div>
  )
}
