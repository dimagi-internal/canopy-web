import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { getAgentSharedVaults, type AgentSharedVaultOut } from '@/api/agents'
import { Td } from '@/components/DataTable'
import { LoadFailed } from '@/components/vault/LoadFailed'
import { vaultHref } from '@/lib/scopedLinks'

// The shared (workspace) vaults this agent's turns ALSO read, layered under its own: each
// one, the names it holds and why the agent gets it, linking to that vault's own page. Without
// it an agent's page told half the story — eva's never mentioned the Salesforce login it reads.

const WHY: Record<string, (name: string) => string> = {
  agent: () => 'given to this agent',
  project: (n) => `through project ${n}`,
  workspace: (n) => `through workspace ${n}`,
}

export function AgentSharedVaults({ slug, onLoaded }: {
  slug: string
  onLoaded?: (vaults: AgentSharedVaultOut[]) => void
}) {
  const [vaults, setVaults] = useState<AgentSharedVaultOut[] | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let live = true
    getAgentSharedVaults(slug)
      .then((v) => { if (live) { setVaults(v); onLoaded?.(v) } })
      .catch(() => { if (live) setFailed(true) })
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug])
  if (failed) return <LoadFailed what="the shared vaults it reads" />
  if (!vaults || vaults.length === 0) return null
  return (
    <div className="mt-5" data-testid="agent-shared-vaults">
      <h4 className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Also from shared vaults</h4>
      <p className="mb-2 text-[12px] text-muted-foreground">
        Its turns also read these. A name here is overridden by the same name above.
      </p>
      <table className="w-full text-[12px]">
        <tbody className="divide-y divide-border">
          {vaults.map((v) => (
            <tr key={v.vault_id} data-testid={`agent-shared-vault-${v.vault_id}`}>
              <Td density="cozy">
                <Link to={vaultHref(v.workspace, v.vault_id)} className="text-primary hover:underline">{v.name}</Link>
              </Td>
              <Td density="cozy" tone="foreground" mono>{(v.secrets ?? []).join(', ') || 'nothing yet'}</Td>
              <Td density="cozy" tone="muted">{(WHY[v.via] ?? WHY.agent)(v.via_name)}</Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
