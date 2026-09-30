import type { JSX } from 'react'

// The one rendering of "this box's owner declared it ZDR", shared by the routing
// rules and the default runner list so the two cannot drift apart.
export function hasZdr(runner: { flags?: readonly string[] | null } | undefined): boolean {
  return Boolean(runner?.flags?.includes('zdr'))
}

export function ZdrBadge({ testId }: { testId: string }): JSX.Element {
  return (
    <span
      data-testid={testId}
      title="Declared ZDR by its owner"
      className="rounded border border-info/30 bg-info/10 px-1 text-[10px] text-info"
    >
      ZDR
    </span>
  )
}
