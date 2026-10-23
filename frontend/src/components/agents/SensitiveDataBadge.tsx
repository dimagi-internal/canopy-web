import type { JSX } from 'react'

// The one rendering of "this box's owner declared it a sensitive-data runner",
// shared by the routing rules and the default runner list so the two cannot drift apart.
export function hasSensitiveData(runner: { flags?: readonly string[] | null } | undefined): boolean {
  return Boolean(runner?.flags?.includes('sensitive_data'))
}

export function SensitiveDataBadge({ testId }: { testId: string }): JSX.Element {
  return (
    <span
      data-testid={testId}
      title="Declared a sensitive-data runner by its owner: a ZDR API key, a locked harness, egress limited to an allowlist"
      className="rounded border border-info/30 bg-info/10 px-1 text-[10px] text-info"
    >
      Sensitive data
    </span>
  )
}
