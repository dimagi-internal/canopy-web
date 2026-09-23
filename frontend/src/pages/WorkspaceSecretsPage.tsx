import { useEffect, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'

import { getSharedVault, setSharedVault, WorkspaceApiError } from '@/api/workspaces'
import { listAgents, type AgentOut } from '@/api/agents'

// THE TENANT HALF of the credential model, and the only screen that states the
// model whole.
//
// Where secrets live has three answers and they are easy to confuse — the vault
// config lived on an agent page, the tenant's lived in an API nobody could reach
// from the app, and the box's own bundle lives on a runner page under a
// different vocabulary. Somebody reasoning about "where does this password
// live" had to hold all three in their head from three screens, which is how a
// whole workspace ran for weeks with four agents unregistered.
//
// The rule this page exists to make obvious: 1PASSWORD HOLDS THE SECRETS, a
// SERVICE ACCOUNT opens a vault, and CANOPY-WEB HOLDS THE SERVICE ACCOUNT —
// nothing else. Two vaults, two keys, one per level.

type VaultState = { vault: string; key_set: boolean }

export function WorkspaceSecretsPage(): JSX.Element {
  const { workspace: slug = '' } = useParams()
  const [state, setState] = useState<VaultState | null>(null)
  const [vault, setVault] = useState('')
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [agents, setAgents] = useState<AgentOut[]>([])

  useEffect(() => {
    let off = false
    setForbidden(false)
    getSharedVault(slug)
      .then((v) => {
        if (off) return
        setState({ vault: v.vault ?? '', key_set: Boolean(v.key_set) })
        setVault(v.vault ?? '')
      })
      .catch((e: unknown) => {
        if (off) return
        // Owner-only server-side. Saying so beats an empty form that 403s on save.
        if (e instanceof WorkspaceApiError && (e.status === 403 || e.status === 404)) setForbidden(true)
        else setError(e instanceof Error ? e.message : 'Could not load the shared vault')
      })
    // Which agents belong to this tenant — the list is workspace-scoped server
    // side, but filter anyway rather than trust a header to have been sent.
    listAgents()
      .then((page) => { if (!off) setAgents(page.items.filter((a) => a.workspace === slug)) })
      .catch(() => {})
    return () => { off = true }
  }, [slug])

  async function save() {
    setBusy(true)
    setSaved(false)
    setError(null)
    try {
      // Non-clobbering, like every write-only field in the app: a blank key is
      // omitted so saving a renamed vault cannot wipe the key that reads it.
      const body: { vault?: string; service_key?: string } = { vault: vault.trim() }
      if (key.trim()) body.service_key = key.trim()
      const v = await setSharedVault(slug, body)
      setState({ vault: v.vault ?? '', key_set: Boolean(v.key_set) })
      setKey('')
      setSaved(true)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not save')
    } finally {
      setBusy(false)
    }
  }

  if (forbidden) {
    return (
      <p className="text-[13px] text-muted-foreground" data-testid="secrets-forbidden">
        Only a workspace owner can see or change this workspace&rsquo;s shared vault.
      </p>
    )
  }

  const withVault = agents.filter((a) => a.slug)
  return (
    <div className="flex flex-col gap-5" data-testid="workspace-secrets">
      <section>
        <h2 className="text-[15px] font-semibold text-foreground">Secrets</h2>
        <p className="mt-1 max-w-2xl text-[13px] text-foreground-secondary">
          Secrets themselves live in <strong>1Password</strong>. A <strong>service account</strong>{' '}
          opens one vault, and canopy-web stores that service account so a runner can fetch it when
          it provisions an agent. canopy-web holds the keys, not the passwords.
        </p>
      </section>

      <section className="rounded-lg border border-border bg-card p-3" data-testid="shared-vault">
        <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          This workspace&rsquo;s shared vault
        </h3>
        <p className="mt-1 max-w-2xl text-[12px] text-muted-foreground">
          What <em>every</em> agent here shares — the Google OAuth clients their mailboxes
          authenticate with, the GitHub token their repos are cloned with. One vault, one key,
          used by every agent in this workspace.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <label className="text-[12px] text-muted-foreground" htmlFor="shared-vault-name">
            Vault
          </label>
          <input
            id="shared-vault-name"
            value={vault}
            onChange={(e) => setVault(e.target.value)}
            placeholder="Canopy-Shared"
            className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground sm:min-h-0 sm:w-52"
          />
          <label className="text-[12px] text-muted-foreground" htmlFor="shared-vault-key">
            Service account
          </label>
          <input
            id="shared-vault-key"
            type="password"
            autoComplete="off"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder={state?.key_set ? 'set — paste to rotate' : 'paste to set'}
            className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground sm:min-h-0 sm:w-56"
            data-testid="shared-vault-key"
          />
          <button
            type="button"
            onClick={() => void save()}
            disabled={busy}
            data-testid="shared-vault-save"
            className="ml-auto min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0"
          >
            {busy ? 'Saving…' : 'Save'}
          </button>
        </div>
        <p className="mt-2 text-[12px]" data-testid="shared-vault-state">
          {state?.key_set ? (
            <span className="text-success">● A service account is stored for this vault.</span>
          ) : (
            <span className="text-warning">
              ○ No service account stored — agents here cannot read anything from the shared vault,
              so their mailboxes will not authenticate.
            </span>
          )}
        </p>
        <p className="mt-2 max-w-2xl text-[11px] text-muted-foreground">
          Scope this service account to the shared vault and nothing else. One that could also read
          the per-agent vaults would undo the reason those are separate. Encrypted at rest and never
          shown again — paste a new one to rotate.
        </p>
        {saved && !error && (
          <p className="mt-2 text-[12px] text-success" data-testid="shared-vault-saved">
            Saved. A runner picks this up on its next refresh.
          </p>
        )}
        {error && (
          <p className="mt-2 text-[12px] text-destructive" data-testid="shared-vault-error">{error}</p>
        )}
      </section>

      <section data-testid="agent-vaults">
        <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          Each agent&rsquo;s own vault
        </h3>
        <p className="mt-1 max-w-2xl text-[12px] text-muted-foreground">
          An agent&rsquo;s own secrets — its canopy token, its mailbox token, anything only it uses —
          live in its own vault with its own service account, so a leaked key reaches one agent
          rather than all of them. Set those on each agent, under Overview → Credentials.
        </p>
        {withVault.length === 0 ? (
          <p className="mt-2 text-[12px] text-muted-foreground">No agents in this workspace yet.</p>
        ) : (
          <ul className="mt-2 flex flex-wrap gap-2">
            {withVault.map((a) => (
              <li key={a.slug}>
                <Link
                  to={`/w/${slug}/agents/${a.slug}/overview#credentials`}
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
          A box has its own credentials — its Claude login and the GitHub token it clones with. Those
          are neither of the above: they belong to the machine, not to a tenant or an agent, and are
          set on the runner (Supervisor → Runners → the box). A runner holds no 1Password key of its
          own; it is handed the two above, per agent, as it provisions one.
        </p>
      </section>
    </div>
  )
}
