import { useState, type JSX } from 'react'
import { Check, Copy } from 'lucide-react'
import { Dialog, DialogContent, DialogTitle } from 'canopy-ui'

import { cloneCommand, repoFolder } from './repoSetup'

/** The steps that put an agent's repo on a laptop runner, prefilled. Nothing is
 *  fetched for you: the clone runs on that machine, by whoever sits at it. */
export function RepoSetupDialog({
  open,
  onClose,
  agentSlug,
  agentName,
  repoUrl,
  runners,
}: {
  open: boolean
  onClose: () => void
  agentSlug: string
  agentName: string
  repoUrl: string
  /** The laptops that do not have it. */
  runners: readonly string[]
}): JSX.Element {
  const command = cloneCommand(agentSlug, repoUrl)
  const [copied, setCopied] = useState(false)
  const copy = () => {
    if (!command) return
    void navigator.clipboard?.writeText(command).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    })
  }
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
          {runners.length === 1 ? 'it' : 'them'}. On {runners.length === 1 ? 'that box' : 'each box'}, signed in as
          its macOS account:
        </p>
        <ol className="m-0 flex list-decimal flex-col gap-2 pl-5 text-[13px] text-foreground-secondary">
          <li>
            {command ? (
              <div className="mt-1 flex items-start gap-2 rounded-md border border-input bg-input p-2">
                <code className="min-w-0 flex-1 break-all font-mono text-[12px] text-foreground" data-testid="repo-setup-command">
                  {command}
                </code>
                <button
                  type="button"
                  onClick={copy}
                  className="inline-flex min-h-8 shrink-0 items-center gap-1 rounded-md border border-border px-2 text-[12px] text-foreground hover:bg-muted"
                >
                  {copied ? <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />}
                  {copied ? 'Copied' : 'Copy'}
                </button>
              </div>
            ) : (
              <span className="text-destructive">
                {agentName} has no repo set, so there is nothing to clone. Set it on the agent&rsquo;s Settings first.
              </span>
            )}
          </li>
          <li>
            In emdash, add <code className="font-mono">{repoFolder(agentSlug)}</code> as a project, named{' '}
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
