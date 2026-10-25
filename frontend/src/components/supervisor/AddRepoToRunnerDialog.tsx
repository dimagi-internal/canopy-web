import { useState, type JSX } from 'react'
import { Dialog, DialogContent, DialogTitle } from 'canopy-ui'
import type { AgentOut } from '@/api/agents'
import type { RunnerOut } from '@/api/harness'
import { RunnerProjects } from './RunnerProjects'

/** "Add a repo to a runner" from anywhere that lists runners (/ide): pick one of
 *  the laptops you administer, then add an agent's repo or any repo to it. */
export function AddRepoToRunnerDialog({
  open,
  onClose,
  runners,
  agents,
}: {
  open: boolean
  onClose: () => void
  runners: readonly RunnerOut[]
  agents?: readonly AgentOut[] | null
}): JSX.Element {
  const laptops = runners.filter((r) => r.kind === 'emdash' && r.can_administer)
  const [picked, setPicked] = useState('')
  const runnerId = picked || laptops[0]?.id || ''
  return (
    <Dialog open={open} onOpenChange={(o: boolean) => !o && onClose()}>
      <DialogContent className="w-[calc(100%-2rem)] max-w-xl bg-card" data-testid="add-repo-dialog">
        <DialogTitle className="text-sm font-semibold text-foreground">Add a repo to a runner</DialogTitle>
        {laptops.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">
            None of the laptop runners you can see is one you administer. Its owner, or someone they made an
            administrator, can add repos to it.
          </p>
        ) : (
          <>
            <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
              Runner
              <select
                value={runnerId}
                onChange={(e) => setPicked(e.target.value)}
                className="h-7 rounded-md border border-input bg-input px-1.5 text-[12px] text-foreground"
                data-testid="add-repo-runner"
              >
                {laptops.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.status === 'online' ? '●' : '○'} {r.name}
                  </option>
                ))}
              </select>
            </label>
            {runnerId && <RunnerProjects key={runnerId} runnerId={runnerId} agents={agents} />}
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
