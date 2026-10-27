import type { JSX } from 'react'
import type { RunnerOut } from '@/api/harness'
import { AlertList } from '@/components/alerts/AlertList'
import { Section } from '@/components/objectPage/settingsLayout'
import { RunnerDrills } from '@/components/supervisor/RunnerDrills'
import { RunnerHealth } from '@/components/supervisor/RunnerHealth'
import { RunnerRefreshStatus } from '@/components/supervisor/RunnerRefresh'
import type { RunnerOutletContext } from './RunnerPage'

/** The agent CLIs the box has, as the server reads its report (`RunnerOut.agent_clis`). */
export function runnerClisText(runner: RunnerOut): string {
  return Object.entries(runner.agent_clis ?? {})
    .map(([, c]) => {
      const state = [c.signed_in ? 'signed in' : 'not signed in']
      if (!c.known) state.push('not known to this server')
      else if (c.signed_in && !c.confines) state.push('cannot confine a caller\u2019s turn yet')
      return `${c.label}${c.version ? ` ${c.version}` : ''} (${state.join(', ')})`
    })
    .join(' · ') || '—'
}

/** Real availability, not last-known ready: a stale runner's ready flag is
 *  whatever it reported on its final heartbeat. */
export function runnerBadge(runner: RunnerOut): { text: string; cls: string } {
  const online = runner.status === 'online'
  if (online && runner.ready) return { text: 'available', cls: 'bg-success/15 text-success' }
  if (online) return { text: 'not ready', cls: 'bg-destructive/15 text-destructive' }
  return { text: runner.status || 'offline', cls: 'bg-muted text-muted-foreground' }
}

/** What the box is: the facts every other tab assumes. Also the fleet map's side
 *  panel, which links here for the rest. */
export function RunnerFacts({ runner }: { runner: RunnerOut }): JSX.Element {
  const caps = (runner.capabilities ?? {}) as { agents?: string[]; projects?: string[] }
  const rows: [string, string, string?][] = [
    ['agents', (caps.agents ?? []).join(', ') || '—'],
    ['projects', (caps.projects ?? []).join(', ') || '—'],
    ['kind', runner.kind ?? ''],
    ['agent CLIs', runnerClisText(runner), 'runner-clis'],
  ]
  if (runner.kind !== 'cloud')
    rows.push(['runtime', runner.engine === 'claude-desktop' ? 'Claude desktop' : runner.engine === 'claude-cli' ? 'Claude CLI' : 'emdash'])
  rows.push(['owner', runner.owner_email ?? '—'])
  // The machine it is on: several runners can share one, each its own user there.
  if (runner.machine) rows.push(['machine', runner.machine, 'runner-machine'])
  // Host only matters for emdash (per-macOS-account session reuse); a cloud
  // runner reports its own name there, which says nothing about the machine.
  else if (runner.host && runner.kind !== 'cloud') rows.push(['host', runner.host])
  // What code it is on. The cloud runner is a different program and reports neither.
  if (runner.code_version) {
    rows.push(['runner code', `${runner.code_version}${runner.code_sha ? ` (${runner.code_sha.slice(0, 12)})` : ''}`])
  }
  if (runner.code_branch) rows.push(['branch', runner.code_branch])
  rows.push(['status', runner.status ?? 'unknown'])
  return (
    <table className="w-full text-[13px]" data-testid="runner-facts">
      <tbody>
        {rows.map(([label, value, testId]) => (
          <tr key={label} className="border-b border-border last:border-b-0">
            <th scope="row" className="w-32 py-1.5 pr-3 text-left text-[11px] font-normal uppercase tracking-wide text-muted-foreground">
              {label}
            </th>
            <td className="min-w-0 truncate py-1.5 text-foreground" data-testid={testId}>{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** Is it working: what it is, what to act on, what it says about itself, and
 *  whether it can run the agents it should. */
export function RunnerOverview({ runner, agents }: RunnerOutletContext): JSX.Element {
  const online = runner.status === 'online'
  return (
    <div data-testid={`runner-detail-${runner.name}`}>
      {online && !runner.ready && runner.ready_note && (
        <p className="mb-4 text-[12px] text-destructive" data-testid="runner-detail-why">{runner.ready_note}</p>
      )}
      {/* What to act on (its credential, a waiting device key…), named the way
          people named things. Nothing when there is nothing to do. */}
      <AlertList scope={`runner:${runner.id}`} hideEmpty className="mb-6" />
      {/* A Refresh someone asked for: what it waits on, or what it changed. */}
      <RunnerRefreshStatus runner={runner} />
      <Section id="about" title="About">
        <RunnerFacts runner={runner} />
      </Section>
      <Section id="health" title="Health">
        <RunnerHealth
          runner={runner}
          agentWorkspace={(slug) => agents?.find((a) => a.slug === slug)?.workspace ?? undefined}
        />
      </Section>
      {/* Readiness checks are runner-ADMIN: the owner, or anyone they granted it. */}
      {(runner.can_administer || runner.can_manage) && (
        <Section id="readiness" title="Readiness">
          <RunnerDrills runnerId={runner.id} />
        </Section>
      )}
      {!runner.can_manage && (
        <p className="text-[12px] text-muted-foreground" data-testid="runner-detail-readonly">
          {runner.can_administer
            ? `You can sign this box in, set its credentials, declare what it runs with (sensitive data)
               and check its readiness for the agents you administer. Pausing or retiring it
               belongs to its owner, ${runner.owner_email ?? 'someone else'}.`
            : `Read-only — this runner is owned by ${runner.owner_email ?? 'someone else'},
               who can check its readiness or grant you administration; they or a runner admin
               can change what it declares.`}
        </p>
      )}
    </div>
  )
}
