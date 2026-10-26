/** Agent CLIs — the program a turn runs in (`apps/harness/clis.py`). The list is
 *  the server's: `Cli` is the generated union, and `LABELS` must name every
 *  member of it, so a new CLI on the server fails this file's type check until it
 *  has a label here. Which CLIs a runner has, signed in and confining, the server
 *  works out (`RunnerOut.agent_clis`). */
import type { components } from '@/api/generated'

export type Cli = components['schemas']['AgentClisIn']['clis'][number]

const LABELS: Record<Cli, string> = { 'claude-code': 'Claude Code', codex: 'Codex' }

export const CLIS = Object.keys(LABELS) as Cli[]

/** A CLI as a person reads it; an unknown value as it is. */
export function cliLabel(cli: string): string {
  return (LABELS as Record<string, string>)[cli] ?? cli
}
