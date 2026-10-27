import type { JSX } from 'react'
import { Dialog, DialogContent, DialogTitle } from 'canopy-ui'

import { AddAgentRepoButton } from '@/components/supervisor/RunnerProjects'
import { CopyCommand } from './CopyCommand'
import { cloneCommand, repoFolder } from './repoSetup'

/** Put an agent's repo on a laptop runner: ask the laptop to do it itself (it
 *  clones the repo and adds it to emdash, `runner_projects`), or follow the
 *  prefilled steps at that machine. */
export function RepoSetupDialog({
  open,
  onClose,
  agentSlug,
  agentName,
  repoUrl,
  runners,
  runnerIds = [],
}: {
  open: boolean
  onClose: () => void
  agentSlug: string
  agentName: string
  repoUrl: string
  /** The laptops that do not have it. */
  runners: readonly string[]
  /** Their ids, in the same order: each gets an "Add it there now". */
  runnerIds?: readonly string[]
}): JSX.Element {
  const command = cloneCommand(agentSlug, repoUrl)
  const windowsCommand = cloneCommand(agentSlug, repoUrl, 'powershell')
  return (
    <Dialog open={open} onOpenChange={(o: boolean) => !o && onClose()}>
      <DialogContent className="w-[calc(100%-2rem)] max-w-lg bg-card" data-testid="repo-setup-dialog">
        <DialogTitle className="text-sm font-semibold text-foreground">
          Put {agentName}&rsquo;s repo on {runners.join(', ')}
        </DialogTitle>
        <p className="text-[13px] text-foreground-secondary">
          {runners.length === 1 ? 'This laptop' : 'These laptops'} can&rsquo;t take {agentName}&rsquo;s work: a laptop
          runs an agent in the emdash project named <code className="font-mono">{agentSlug}</code>, and{' '}
          {runners.length === 1 ? 'it has' : 'they have'} none. Until then {agentName}&rsquo;s work skips{' '}
          {runners.length === 1 ? 'it' : 'them'}.
        </p>
        {command && runnerIds.length > 0 && (
          <div className="flex flex-col gap-1 rounded-md border border-border bg-muted/30 p-2" data-testid="repo-setup-ask">
            <p className="m-0 text-[12px] text-foreground-secondary">
              Have {runnerIds.length === 1 ? 'it' : 'each'} add it: the runner clones the repo under its own account
              and adds it to emdash as <code className="font-mono">{agentSlug}</code>, then reports back here.
            </p>
            {runnerIds.map((id, i) => (
              <div key={id} className="flex flex-wrap items-baseline gap-2 text-[12px]">
                <span className="font-mono text-foreground">{runners[i] ?? id}</span>
                <AddAgentRepoButton runnerId={id} agentSlug={agentSlug} />
              </div>
            ))}
          </div>
        )}
        <p className="text-[13px] text-foreground-secondary">
          {runnerIds.length > 0 ? 'Or by hand: on' : 'On'} {runners.length === 1 ? 'that box' : 'each box'}, signed in
          as the account its runner runs under:
        </p>
        <ol className="m-0 flex list-decimal flex-col gap-2 pl-5 text-[13px] text-foreground-secondary">
          <li>
            {command && windowsCommand ? (
              <div className="flex flex-col gap-1">
                <span className="text-[12px] text-muted-foreground">macOS or Linux (Terminal)</span>
                <CopyCommand command={command} testId="repo-setup-command" />
                <span className="text-[12px] text-muted-foreground">Windows (PowerShell)</span>
                <CopyCommand command={windowsCommand} testId="repo-setup-command-windows" />
              </div>
            ) : (
              <span className="text-destructive">
                {agentName} has no repo set, so there is nothing to clone. Set it on the agent&rsquo;s Settings first.
              </span>
            )}
          </li>
          <li>
            In emdash, add that folder (<code className="font-mono">{repoFolder(agentSlug)}</code>, or{' '}
            <code className="font-mono">{repoFolder(agentSlug, 'powershell')}</code> on Windows) as a project, named{' '}
            <code className="font-mono">{agentSlug}</code>.
          </li>
        </ol>
        <p className="text-[12px] text-muted-foreground">
          The runner reports its projects every few seconds, so the box starts taking {agentName}&rsquo;s work on its
          own once the project exists.
        </p>
      </DialogContent>
    </Dialog>
  )
}
