import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Button } from 'canopy-ui/ui'

// The one-time recovery code, shown where the person is looking: the code, a download,
// "I have saved it", Continue. The code is canopy-web's only chance to hand it over — nobody
// keeps a copy — so Continue waits for the box to be ticked. It is NOT a modal (the page stays
// usable), but it takes focus when it appears so a keyboard or screen-reader user lands on it.

/** Save `text` as a file the browser downloads. */
export function downloadText(filename: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export function RecoveryCodeBox({
  title,
  children,
  code,
  filename,
  fileText,
  busy = false,
  onContinue,
  onCancel,
  testId,
  savedTestId,
  continueTestId,
}: {
  title: string
  /** Why this code matters, in a sentence or two. */
  children: ReactNode
  code: string
  filename: string
  fileText: string
  busy?: boolean
  onContinue: () => void
  onCancel?: () => void
  testId: string
  savedTestId: string
  continueTestId: string
}) {
  const [saved, setSaved] = useState(false)
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    box.current?.focus()
    box.current?.scrollIntoView?.({ block: 'nearest' })
  }, [])
  return (
    <div ref={box} tabIndex={-1} role="group" aria-label="Recovery code" data-testid={testId}
         className="space-y-2 rounded-lg border border-warning/40 bg-warning/10 p-3 text-[12px] outline-none">
      <p className="font-medium text-foreground">{title}</p>
      <p className="text-muted-foreground">{children}</p>
      <code className="block select-all break-all rounded border border-border bg-card px-2 py-1 font-mono text-[13px] text-foreground">
        {code}
      </code>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" onClick={() => downloadText(filename, fileText)}>Download</Button>
        <label className="flex items-center gap-1 text-foreground">
          <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} data-testid={savedTestId} />
          I have saved it
        </label>
        <Button size="sm" disabled={!saved || busy} onClick={onContinue} data-testid={continueTestId}>Continue</Button>
        {onCancel && <Button size="sm" variant="ghost" onClick={onCancel}>Cancel</Button>}
      </div>
    </div>
  )
}
