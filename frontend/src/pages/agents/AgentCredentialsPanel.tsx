import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  deleteAgentCredential,
  getAgentCredentialStatus,
  setAgentCredentials,
  startGoogleMint,
  type AgentCredentialStatusOut,
} from '@/api/agents'
import { headline, sections, type CredStatus } from '@/pages/agents/agentCredentials'
import { relativeAge } from '@/lib/relativeAge'
import { declaresMailbox, GOG_TOKEN_REF, mintOutcome } from '@/pages/agents/googleMint'
import { AgentGitHubSection } from '@/pages/agents/AgentGitHubSection'
import { AgentSalesforceSection } from '@/pages/agents/AgentSalesforceSection'
import { AgentVaultSection } from '@/pages/agents/AgentVaultSection'
import { WorkbenchSkeleton } from 'canopy-ui'

// "What is stopping this agent from running" — a question that on 2026-09-05
// cost an SSH to a box and a `gog auth list`. ACE's mailbox had been dead since
// May under an OAuth client its own config warns against, and nothing surfaced
// it until a readiness drill happened to run three weeks later.
//
// Values are write-only: the status route returns booleans and timestamps and
// never plaintext, so this page can say "set" and "when" and cannot render a
// secret. A blank field is not sent — the write is non-clobbering, and "" would
// wipe a working credential.
//
// It LEADS with a status list (#1314): one line per credential — vault, GitHub,
// Salesforce, Google mailbox — each reported up by its own detail section, so
// the one that needs a person (a mailbox nobody connected) is the first thing
// on screen rather than the last, with its action on the same line.
//
// A panel, not a page: it lives in the Credentials section of the agent's
// Overview (it used to be its own rail entry, which is why people could not
// find settings that sat one click away from each other).

const TONE: Record<CredStatus['tone'], string> = {
  success: 'bg-success/10 text-success border-success/30',
  warning: 'bg-warning/10 text-warning border-warning/30',
  destructive: 'bg-destructive/10 text-destructive border-destructive/30',
  muted: 'bg-muted text-muted-foreground border-border',
}

const MARK: Record<CredStatus['tone'], string> = { success: '✓', warning: '!', destructive: '✗', muted: '–' }

type StatusKey = 'vault' | 'github' | 'salesforce'

export function AgentCredentialsPanel({
  agent,
  canEdit = true,
}: {
  agent: { slug: string; name?: string; workspace?: string | null; email?: string | null }
  /** Display hint: says who may change values when the viewer cannot. */
  canEdit?: boolean
}) {
  const [rows, setRows] = useState<AgentCredentialStatusOut[] | null>(null)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [params] = useSearchParams()
  const outcome = mintOutcome(params.get('google'))
  // Collapsed by DEFAULT. These rows are correct and need nothing; shown as a
  // list of empty inputs they read as forty-five things to type.
  const [showVault, setShowVault] = useState(false)
  const [status, setStatus] = useState<Partial<Record<StatusKey, CredStatus>>>({})
  const report = useCallback(
    (key: StatusKey) => (s: CredStatus) => setStatus((prev) => ({ ...prev, [key]: s })),
    [],
  )
  // Stable per key, so a section's effect does not re-fire on every render.
  const [onVault] = useState(() => report('vault'))
  const [onGitHub] = useState(() => report('github'))
  const [onSalesforce] = useState(() => report('salesforce'))

  useEffect(() => {
    let cancelled = false
    setRows(null)
    setDraft({})
    getAgentCredentialStatus(agent.slug)
      .then((r) => !cancelled && setRows(r))
      .catch((e: unknown) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : 'Failed to load')
        setRows([])
      })
    return () => {
      cancelled = true
    }
  }, [agent.slug])

  const save = async (name: string) => {
    const value = (draft[name] ?? '').trim()
    if (!value) return
    setBusy(true)
    setError(null)
    try {
      setRows(await setAgentCredentials(agent.slug, { [name]: value }))
      // Never keep a secret in component state once it has landed.
      setDraft((d) => ({ ...d, [name]: '' }))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save')
    } finally {
      setBusy(false)
    }
  }

  const connectMailbox = async () => {
    setBusy(true)
    setError(null)
    try {
      // A TOP-LEVEL navigation, deliberately. Following the redirect inside
      // fetch() lands on Google's HTML with nothing shown to the user; only
      // leaving the page can render a consent screen.
      window.location.assign(await startGoogleMint(agent.slug))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not start the Google sign-in')
      setBusy(false)
    }
  }

  const remove = async (name: string) => {
    setBusy(true)
    setError(null)
    try {
      setRows(await deleteAgentCredential(agent.slug, name))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to remove')
    } finally {
      setBusy(false)
    }
  }

  if (rows === null) {
    return <WorkbenchSkeleton />
  }

  // An agent with a mailbox needs `gog-token` whether or not a runtime.yaml says so.
  const implied = agent.email?.trim() ? [GOG_TOKEN_REF] : []
  const sec = sections(rows, implied)
  const hasMailbox = declaresMailbox(rows, agent.email)
  const gog = rows.find((r) => r.name === GOG_TOKEN_REF)
  const mailbox: CredStatus = gog?.set
    ? { label: 'Connected', tone: 'success' }
    : gog
      ? { label: 'From 1Password', tone: 'muted' }
      : { label: 'Not connected', tone: 'destructive' }

  const statusRows: { key: string; name: string; href?: string; s: CredStatus | undefined }[] = [
    { key: 'vault', name: '1Password vault', href: '#cred-vault', s: status.vault },
    { key: 'github', name: 'GitHub', href: '#cred-github', s: status.github },
    ...(status.salesforce ? [{ key: 'salesforce', name: 'Salesforce', href: '#cred-salesforce', s: status.salesforce }] : []),
    ...(hasMailbox ? [{ key: 'mailbox', name: 'Google mailbox', s: mailbox }] : []),
  ]

  // One row, used for anything this page actually manages. The vault-resolved
  // refs get the same control once expanded — a value CAN be stored here to
  // override the vault, it just isn't the normal case and shouldn't lead.
  const credentialRow = (r: AgentCredentialStatusOut) => (
    <div
      key={r.name}
      className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card px-3 py-2"
      data-testid={`cred-${r.name}`}
    >
      <span className={r.set ? 'text-success' : 'text-muted-foreground'}>{r.set ? '●' : '○'}</span>
      <span className="font-mono text-[12px] text-foreground">{r.name}</span>

      {!r.declared && (
        <span className="rounded border border-destructive px-1 text-[10px] uppercase text-destructive">
          undeclared
        </span>
      )}
      {r.set && (
        <span className="text-[11px] text-muted-foreground">
          {r.source}
          {r.updated_at ? ` · ${relativeAge(r.updated_at)}` : ''}
          {r.updated_by_email ? ` · ${r.updated_by_email}` : ''}
        </span>
      )}

      <input
        type="password"
        autoComplete="off"
        value={draft[r.name] ?? ''}
        onChange={(e) => setDraft((d) => ({ ...d, [r.name]: e.target.value }))}
        placeholder={r.set ? 'rotate…' : 'store here instead'}
        aria-label={`Value for ${r.name}`}
        className="ml-auto min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground placeholder:text-muted-foreground sm:min-h-0 sm:w-56"
      />
      <button
        type="button"
        onClick={() => void save(r.name)}
        disabled={busy || !(draft[r.name] ?? '').trim()}
        className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0"
      >
        Save
      </button>
      {r.set && (
        // Destroys a secret nothing else holds — canopy-web is the store, so
        // there is no undo and no second copy to restore from. It was a 20×24
        // target whose only description was the glyph: `title` now names the
        // consequence the way the chat list's close button does, the hit area
        // clears 44px on touch, and it asks first.
        <button
          type="button"
          onClick={() => {
            if (window.confirm(`Delete the stored value for "${r.name}"? This cannot be undone.`)) {
              void remove(r.name)
            }
          }}
          disabled={busy}
          aria-label={`Delete the stored value for ${r.name}`}
          title={`Delete the stored value for "${r.name}" (cannot be undone)`}
          className="flex min-h-11 w-11 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive disabled:opacity-40 sm:min-h-0 sm:h-7 sm:w-7"
        >
          ✕
        </button>
      )}
    </div>
  )

  return (
    <div data-testid="agent-credentials">

      {outcome && (
        <p
          className={`mb-3 text-[13px] ${outcome.tone === 'ok' ? 'text-success' : 'text-destructive'}`}
          data-testid="google-mint-outcome"
        >
          {outcome.text}
        </p>
      )}

      <table className="mb-6 w-full text-[12px]" data-testid="credential-status">
        <tbody className="divide-y divide-border">
          {statusRows.map((r) => (
            <tr key={r.key} data-testid={`credential-status-${r.key}`}>
              <td className="w-40 py-1.5 text-foreground">
                {r.href ? (
                  <a href={r.href} className="hover:text-primary">
                    {r.name}
                  </a>
                ) : (
                  r.name
                )}
              </td>
              <td className="py-1.5">
                {r.s ? (
                  <span className={`rounded border px-1.5 py-0.5 text-[11px] ${TONE[r.s.tone]}`}>
                    {MARK[r.s.tone]} {r.s.label}
                  </span>
                ) : (
                  <span className="text-[11px] text-muted-foreground">…</span>
                )}
              </td>
              <td className="py-1.5 text-right">
                {r.key === 'mailbox' && (
                  // Signing in is the only way a mailbox token is minted, so the
                  // action sits on its own status line.
                  <button
                    type="button"
                    onClick={() => void connectMailbox()}
                    disabled={busy}
                    className={`min-h-11 rounded-md px-3 py-1 text-[12px] font-medium disabled:opacity-40 sm:min-h-0 ${
                      gog?.set
                        ? 'border border-input text-foreground hover:border-primary'
                        : 'bg-primary text-primary-foreground hover:bg-primary/90'
                    }`}
                  >
                    Connect Google mailbox
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* ALWAYS, and above the declaration-dependent half. Which vault this agent
          reads and the service account that opens it are facts about the agent,
          not about its runtime.yaml — and they were rendered inside the
          "declares some secrets" branch, so the form was hidden on exactly the
          agents nobody had registered yet (2026-09-23). */}
      <AgentVaultSection slug={agent.slug} workspace={agent.workspace} onStatus={onVault} />
      <AgentGitHubSection slug={agent.slug} onStatus={onGitHub} />
      <AgentSalesforceSection slug={agent.slug} onStatus={onSalesforce} />

      {/* Zero refs is UNDECLARED, not provisioned: nothing to list. Saying
          "ready" would assert a box can run it, which nobody has established;
          the explanation of runtime.yaml that sat here belongs in docs (#1314). */}
      {rows.length === 0 ? null : (
        <>
          {/* The lede: whether this screen wants anything from the reader. */}
          <p className="mb-4 text-[13px] text-muted-foreground" data-testid="agent-credentials-summary">
            {headline(rows, implied)}
          </p>


          {sec.orphans.length > 0 && (
            // The only thing on this page that is actually WRONG: a live secret
            // nothing declares any more. It must not sit below forty-five
            // healthy rows.
            <section className="mb-5" data-testid="orphans">
              <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-destructive">
                Stored here, no longer declared
              </h3>
              <div className="space-y-2">{sec.orphans.map(credentialRow)}</div>
            </section>
          )}

          {sec.storedHere.length > 0 && (
            <section className="mb-5">
              <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                Stored in canopy-web
              </h3>
              <div className="space-y-2">{sec.storedHere.map(credentialRow)}</div>
            </section>
          )}

          {sec.fromVault.length > 0 && (
            <section>
              <button
                type="button"
                onClick={() => setShowVault((v) => !v)}
                className="inline-flex min-h-11 items-center text-[12px] text-muted-foreground underline-offset-2 hover:underline sm:min-h-0"
                data-testid="toggle-vault-refs"
                aria-expanded={showVault}
              >
                {showVault ? '▾' : '▸'} {sec.fromVault.length} resolve from 1Password on the
                box {showVault ? '' : '— nothing to do'}
              </button>
              {showVault && (
                <>
                  <p className="mt-2 text-[12px] text-muted-foreground">
                    The runner reads these with this agent’s own vault key (the Vault section above). Storing one here
                    would override the vault for this agent — useful for a value the vault does not
                    have, and a second copy to keep in step otherwise.
                  </p>
                  <div className="mt-2 space-y-2">{sec.fromVault.map(credentialRow)}</div>
                </>
              )}
            </section>
          )}
        </>
      )}

      {error && (
        <p className="mt-3 text-[13px] text-destructive" data-testid="agent-credentials-error">
          {error}
        </p>
      )}

      {/* The one note on how values are held; the vault and GitHub sections
          each used to repeat it. */}
      <p className="mt-4 text-[11px] text-muted-foreground" data-testid="credentials-note">
        Every value here is write-only: encrypted at rest, handed only to a runner this agent routes to, and never
        shown again.{!canEdit && <> Only {agent.name ?? agent.slug}&rsquo;s admins can change them.</>}
      </p>
    </div>
  )
}
