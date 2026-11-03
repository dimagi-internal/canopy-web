import { type JSX } from 'react'
import { useAuth } from '@/auth/AuthProvider'
import { RunnerDownloads } from '@/components/runners/RunnerDownloads'
import { pairingCommand, type FleetMap } from '@/pages/topology/topologyMap'
import { findWorkspace } from '@/pages/topology/panels/shared'

export function AddRunnerPanel({ workspace, owner, map }: { workspace: string; owner: string; map: FleetMap }): JSX.Element {
  const auth = useAuth()
  const me = auth.user?.email ?? null
  const isMe = me !== null && me === owner
  const base = `${window.location.origin}${import.meta.env.BASE_URL}`
  const command = pairingCommand(workspace, base)
  const wsName = findWorkspace(map.root, workspace)?.displayName ?? workspace
  return (
    <div className="flex flex-col gap-3" data-testid="map-add-runner-panel">
      <div className="flex flex-col gap-1">
        <h3 className="m-0 text-[16px] font-semibold text-foreground">Add a runner</h3>
        <span className="text-[12px] text-muted-foreground">
          {wsName} · {isMe ? 'your lane' : `${owner}'s lane`}
        </span>
      </div>
      <p className="m-0 text-[13px] text-foreground-secondary">
        A runner is paired from the computer itself, one per account on it.{' '}
        {isMe
          ? 'Download Canopy Runner there and approve it as yourself to land it here.'
          : `It lands in ${owner}'s lane only if ${owner} approves it.`}
      </p>
      <RunnerDownloads authed={auth.status === 'authenticated'} developerCommand={command} />
      <p className="m-0 text-[12px] text-muted-foreground">
        It appears here once it heartbeats. Then select it to set its login and admins, and add it to an agent&rsquo;s
        runners. A cloud runner is stood up with <code className="font-mono">runner/ec2/up.sh</code> and{' '}
        <code className="font-mono">wire.sh</code> instead, which need AWS access.
      </p>
    </div>
  )
}
