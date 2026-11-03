import { clsx } from 'clsx'
import type { JSX } from 'react'

// One look for a session's state wherever sessions are listed (/ide, Home, the
// session panel), so "amber = it is asking you, pulsing green = it is running"
// means the same thing on every page. `data-tone` carries the tone for tests.

/** `asking`: a question is put to you. `running`: being worked now. `held`:
 *  waiting behind a runner that is away. `idle`: nothing is moving. */
export type DotTone = 'asking' | 'running' | 'held' | 'idle'

export function StateDot({
  tone,
  title,
  testId,
}: {
  tone: DotTone
  title?: string
  testId?: string
}): JSX.Element {
  return (
    <span
      aria-hidden={title ? undefined : true}
      title={title}
      data-testid={testId}
      data-tone={tone}
      className={clsx(
        'inline-block h-2 w-2 shrink-0 rounded-full',
        tone === 'asking' && 'bg-warning',
        tone === 'running' && 'animate-pulse bg-success',
        tone === 'held' && 'border border-muted-foreground bg-transparent',
        tone === 'idle' && 'bg-muted-foreground/40',
      )}
    />
  )
}
