import { useEffect, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'

import { listAgents, type AgentOut } from '@/api/agents'
import { scopedPath } from '../lib/scopedLinks'
import { WorkspaceVaultsSection } from './WorkspaceVaultsSection'

// THE TENANT HALF of the credential model, and the only screen that states the
// model whole: where every secret an agent uses lives.
//
// Three places, one meaning each: a WORKSPACE VAULT holds what agents share (the
// Google OAuth clients), attached where it applies and reaching every agent below;
// each AGENT'S OWN VAULT holds what only it uses; a RUNNER holds its own login.
// All three are canopy vaults, zero-knowledge: canopy-web stores ciphertext and the
// copies of each vault's key, never a password, and no 1Password key at all.

export function WorkspaceSecretsPage(): JSX.Element {
  const { workspace: slug = '' } = useParams()
  const [agents, setAgents] = useState<AgentOut[]>([])

  useEffect(() => {
    let off = false
    // Which agents belong to this tenant — the list is workspace-scoped server
    // side, but filter anyway rather than trust a header to have been sent.
    listAgents()
      .then((page) => { if (!off) setAgents(page.items.filter((a) => a.workspace === slug)) })
      .catch(() => {})
    return () => { off = true }
  }, [slug])

  const withVault = agents.filter((a) => a.slug)
  return (
    <div className="flex flex-col gap-5" data-testid="workspace-secrets">
      <section>
        <h2 className="text-[15px] font-semibold text-foreground">Secrets</h2>
        <p className="mt-1 max-w-2xl text-[13px] text-foreground-secondary">
          Every secret an agent uses lives in a <strong>canopy vault</strong>: shared ones in a
          workspace vault, the agent&rsquo;s own in its own vault. canopy-web stores only ciphertext;
          the people and machines a vault lists hold its key.
        </p>
      </section>

      <WorkspaceVaultsSection slug={slug} />

      <section data-testid="agent-vaults">
        <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          Each agent&rsquo;s own vault
        </h3>
        <p className="mt-1 max-w-2xl text-[12px] text-muted-foreground">
          An agent&rsquo;s own secrets — its canopy token, its mailbox token, anything only it uses —
          live in its own vault, so what one agent holds reaches no other. Set those on each agent,
          under Settings → Credentials.
        </p>
        {withVault.length === 0 ? (
          <p className="mt-2 text-[12px] text-muted-foreground">No agents in this workspace yet.</p>
        ) : (
          <ul className="mt-2 flex flex-wrap gap-2">
            {withVault.map((a) => (
              <li key={a.slug}>
                <Link
                  to={scopedPath(slug, `/agents/${a.slug}/settings/security`)}
                  className="inline-block rounded-md border border-border px-2 py-1 text-[12px] text-primary hover:bg-muted"
                  data-testid={`agent-vault-link-${a.slug}`}
                >
                  {a.name} →
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section data-testid="runner-credentials-pointer">
        <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          What a runner itself holds
        </h3>
        <p className="mt-1 max-w-2xl text-[12px] text-muted-foreground">
          A box has its own credential — its Claude login. That belongs to the machine, not to a
          tenant or an agent, and is set on the runner (Supervisor → Runners → the box).
        </p>
      </section>
    </div>
  )
}
