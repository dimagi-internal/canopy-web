/** Agent CLIs — the program a turn runs in (`apps/harness/clis.py`). */

export const CLIS = ['claude-code', 'codex'] as const
export type Cli = (typeof CLIS)[number]

const LABELS: Record<string, string> = { 'claude-code': 'Claude Code', codex: 'Codex' }

/** A CLI as a person reads it; an unknown value as it is. */
export function cliLabel(cli: string): string {
  return LABELS[cli] ?? cli
}

// The guard and caller-envelope versions a box must report for a CLI before it
// is handed a caller's turn on it (`routing.PROFILES_VERSION` / `ENVELOPE_VERSION`).
const PROFILES_VERSION = 3
const ENVELOPE_VERSION = 2

export interface RunnerCli {
  cli: string
  signedIn: boolean
  confines: boolean
  version: string
}

type Report = { signed_in?: boolean; profiles?: number; envelope?: number; version?: string }

/** The CLIs a runner reports, as `clis.reported` reads them: a box that reports none
 *  is Claude Code alone, signed in unless its runner says otherwise. */
export function runnerClis(capabilities: Record<string, unknown> | null | undefined, signedIn: boolean): RunnerCli[] {
  const caps = (capabilities ?? {}) as { clis?: Record<string, Report>; profiles?: number; envelope?: number }
  const confines = (r: Report) => (r.profiles ?? 0) >= PROFILES_VERSION && (r.envelope ?? 0) >= ENVELOPE_VERSION
  if (caps.clis && typeof caps.clis === 'object') {
    return Object.entries(caps.clis).map(([cli, r]) => ({
      cli, signedIn: r.signed_in === true, confines: confines(r), version: r.version ?? '',
    }))
  }
  return [{ cli: 'claude-code', signedIn, confines: confines(caps), version: '' }]
}
