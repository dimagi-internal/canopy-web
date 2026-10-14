import { useRef, useState, type JSX } from 'react'
import { Check, Copy } from 'lucide-react'

/** A command to paste into a terminal, with a Copy button. Shared by every place
 *  canopy hands a person something to run on their own machine.
 *
 *  Where the clipboard API is missing or refused (an insecure origin, a
 *  browser that blocks it), the button selects the command instead, so the
 *  person can still copy it by hand. */
export function CopyCommand({ command, testId }: { command: string; testId?: string }): JSX.Element {
  const [copied, setCopied] = useState(false)
  const codeRef = useRef<HTMLElement>(null)
  const selectCommand = () => {
    const el = codeRef.current
    const selection = window.getSelection()
    if (!el || !selection) return
    const range = document.createRange()
    range.selectNodeContents(el)
    selection.removeAllRanges()
    selection.addRange(range)
  }
  const copy = () => {
    const write = navigator.clipboard?.writeText(command)
    if (!write) {
      selectCommand()
      return
    }
    void write.then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    }, selectCommand)
  }
  return (
    <div className="mt-1 flex items-start gap-2 rounded-md border border-input bg-input p-2">
      <code ref={codeRef} className="min-w-0 flex-1 break-all font-mono text-[12px] text-foreground" data-testid={testId}>
        {command}
      </code>
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? `Copied ${command}` : `Copy ${command}`}
        className="inline-flex min-h-8 shrink-0 items-center gap-1 rounded-md border border-border px-2 text-[12px] text-foreground hover:bg-muted"
      >
        {copied ? <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />}
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  )
}
