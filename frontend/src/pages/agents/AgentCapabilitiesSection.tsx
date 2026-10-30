import { useCallback, useEffect, useState, } from 'react'
import { useOutletContext } from 'react-router-dom'
import { WorkbenchSubHeader } from 'canopy-ui'
import {
  getAgentCapabilities,
  type AgentCapabilitiesOut,
  } from '@/api/capabilities'
import { roleAllows } from '@/lib/workspaceRoles'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { useWorkspaceRole } from '@/workspace/WorkspaceProvider'
import { CapabilityRow, LeaseTable } from './CapabilityParts'

// WHAT CLOUD MACHINES THIS AGENT MAY LEASE, and the one place a workspace admin
// says yes (docs/architecture/capabilities.md).
//
// The agent's deploy DECLARES a capability (`canopy-runtime publish-capabilities`)
// and an acquire is refused with 403 until its workspace GRANTS it. Grants were
// API-only (`PUT /api/capabilities/grants`), so an admin had no URL to click.
//
// Grant / Revoke show only for `integrations` (a workspace admin) — the route's
// own gate. Everyone else reads the same state and a line saying who grants. The
// optional limits can only LOWER the manifest's; the server enforces that and
// its refusal text is shown as-is, so the page never second-guesses the rule.
export function AgentCapabilitiesSection() {
  const { agent } = useOutletContext<AgentOutletContext>()
  const canGrant = roleAllows(useWorkspaceRole(agent.workspace), 'integrations')
  const [data, setData] = useState<AgentCapabilitiesOut | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    getAgentCapabilities(agent.slug)
      .then((d) => {
        setData(d)
        setError(null)
      })
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed to load capabilities'))
  }, [agent.slug])
  useEffect(() => load(), [load])

  const caps = data?.capabilities ?? []
  const leases = caps.flatMap((c) => c.live_leases)

  return (
    <div className="max-w-5xl px-6 py-8">
      <WorkbenchSubHeader title="Capabilities" count={data ? caps.length : undefined} />

      <p className="-mt-2 mb-2 text-[13px] text-foreground-secondary">
        Cloud machines and pools {agent.name} declares it can lease. {agent.name} can lease one only after a
        workspace admin grants it in this workspace.
      </p>
      {!canGrant && (
        <p className="mb-6 text-[12px] text-muted-foreground" data-testid="capabilities-readonly">
          Only a workspace admin can grant or revoke a capability.
        </p>
      )}

      {error && (
        <p role="alert" className="mt-4 text-[13px] text-destructive">
          {error}
        </p>
      )}
      {!data && !error && <p className="mt-4 text-[13px] text-muted-foreground">Loading…</p>}

      {data && caps.length === 0 && (
        <p className="mt-4 text-[13px] text-muted-foreground">
          {agent.name} declares no capabilities. Its deploy publishes them with{' '}
          <code className="text-[12px]">canopy-runtime publish-capabilities</code>.
        </p>
      )}

      {caps.length > 0 && (
        <div className="mt-4 overflow-x-auto rounded-lg border border-border bg-card">
          <table className="w-full text-left text-[12px]">
            <thead className="border-b border-border text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Capability</th>
                <th className="px-3 py-2 font-medium">Kind</th>
                <th className="px-3 py-2 font-medium">Where</th>
                <th className="px-3 py-2 font-medium">Limits</th>
                <th className="px-3 py-2 font-medium">Grant</th>
                {canGrant && <th className="px-3 py-2 font-medium" aria-label="Actions" />}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {caps.map((cap) => (
                <CapabilityRow key={cap.name} cap={cap} agentSlug={agent.slug} canGrant={canGrant} onChanged={load} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data && (
        <section aria-labelledby="live-leases-title" className="mt-8">
          <h2 id="live-leases-title" className="mb-3 text-[15px] font-semibold text-foreground">
            Live leases
          </h2>
          {leases.length === 0 ? (
            <p className="text-[13px] text-muted-foreground">Nothing is held or waiting.</p>
          ) : (
            <LeaseTable leases={leases} />
          )}
        </section>
      )}

      {data && data.retired.length > 0 && (
        <details className="mt-8" data-testid="retired-capabilities">
          <summary className="cursor-pointer text-[13px] text-muted-foreground hover:text-foreground">
            Retired ({data.retired.length})
          </summary>
          <p className="mt-2 text-[12px] text-muted-foreground">
            No longer declared by {agent.name}'s deploy; they cannot be leased.
          </p>
          <ul className="mt-1 list-inside list-disc text-[12px] text-foreground-secondary">
            {data.retired.map((name) => (
              <li key={name}>
                <code>{name}</code>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

