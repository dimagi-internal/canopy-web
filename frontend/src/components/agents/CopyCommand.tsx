import { useState, type JSX } from 'react'
import { Check, Copy } from 'lucide-react'

/** A command to paste into a terminal, with a Copy button. Shared by every place
 *  canopy hands a person something to run on their own machine. */
export function CopyCommand({ command, testId }: { command: string; testId?: string }): JSX.Element {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    void navigator.clipboard?.writeText(command).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    })
  }
  return (
    <div className="mt-1 flex items-start gap-2 rounded-md border border-input bg-input p-2">
      <code className="min-w-0 flex-1 font-mono [overflow-wrap:anywhere] text-[12px] text-foreground" data-testid={testId}>
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
  )
}
