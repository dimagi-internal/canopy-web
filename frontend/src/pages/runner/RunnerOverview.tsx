import type { JSX } from 'react'
import type { RunnerOut } from '@/api/harness'
import { AlertList } from '@/components/alerts/AlertList'
import { Section } from '@/components/objectPage/settingsLayout'
import { RunnerDrills } from '@/components/supervisor/RunnerDrills'
import { RunnerHealth } from '@/components/supervisor/RunnerHealth'
import type { RunnerOutletContext } from './RunnerPage'

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
  const rows: [string, string][] = [
    ['agents', (caps.agents ?? []).join(', ') || '—'],
    ['projects', (caps.projects ?? []).join(', ') || '—'],
    ['kind', runner.kind ?? ''],
  ]
  if (runner.kind !== 'cloud') rows.push(['runtime', runner.engine === 'claude-desktop' ? 'Claude desktop' : 'emdash'])
  rows.push(['owner', runner.owner_email ?? '—'])
  // Host only matters for emdash (per-macOS-account session reuse); cloud boxes report none.
  if (runner.host) rows.push(['host', runner.host])
  // What code it is on. The cloud runner is a different program and reports neither.
  if (runner.code_version) {
    rows.push(['runner code', `${runner.code_version}${runner.code_sha ? ` (${runner.code_sha.slice(0, 12)})` : ''}`])
  }
  if (runner.code_branch) rows.push(['branch', runner.code_branch])
  rows.push(['status', runner.status ?? 'unknown'])
  return (
    <table className="w-full text-[13px]" data-testid="runner-facts">
      <tbody>
        {rows.map(([label, value]) => (
          <tr key={label} className="border-b border-border last:border-b-0">
            <th scope="row" className="w-32 py-1.5 pr-3 text-left text-[11px] font-normal uppercase tracking-wide text-muted-foreground">
              {label}
            </th>
            <td className="min-w-0 truncate py-1.5 text-foreground">{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** Is it working: what it is, what to act on, what it says about itself, and
 *  whether it can run the agents it should. */
export function RunnerOverview({ runner, agents, onChanged }: RunnerOutletContext): JSX.Element {
  const online = runner.status === 'online'
  return (
    <div data-testid={`runner-detail-${runner.name}`}>
      {online && !runner.ready && runner.ready_note && (
        <p className="mb-4 text-[12px] text-destructive" data-testid="runner-detail-why">{runner.ready_note}</p>
      )}
      {/* What to act on (its credential, a waiting device key…), named the way
          people named things. Nothing when there is nothing to do. */}
      <AlertList scope={`runner:${runner.id}`} hideEmpty className="mb-6" />
      <Section id="about" title="About">
        <RunnerFacts runner={runner} />
      </Section>
      <Section id="health" title="Health">
        <RunnerHealth
          runner={runner}
          onChanged={onChanged}
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
