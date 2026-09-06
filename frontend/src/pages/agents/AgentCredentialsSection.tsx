import { useEffect, useState } from 'react'
import { useOutletContext, useSearchParams } from 'react-router-dom'
import {
  deleteAgentCredential,
  getAgentCredentialStatus,
  setAgentCredentials,
  startGoogleMint,
  type AgentCredentialStatusOut,
} from '@/api/agents'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { headline, sections } from '@/pages/agents/agentCredentials'
import { declaresMailbox, mintOutcome } from '@/pages/agents/googleMint'
import { AgentVaultSection } from '@/pages/agents/AgentVaultSection'
import { WorkbenchSubHeader, WorkbenchSkeleton } from 'canopy-ui'

// "What is stopping this agent from running" — a question that on 2026-09-05
// cost an SSH to a box and a `gog auth list`. ACE's mailbox had been dead since
// May under an OAuth client its own config warns against, and nothing surfaced
// it until a readiness drill happened to run three weeks later.
//
// Values are write-only: the status route returns booleans and timestamps and
// never plaintext, so this page can say "set" and "when" and cannot render a
// secret. A blank field is not sent — the write is non-clobbering, and "" would
// wipe a working credential.

export function AgentCredentialsSection() {
  const { agent } = useOutletContext<AgentOutletContext>()
  const [rows, setRows] = useState<AgentCredentialStatusOut[] | null>(null)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [params] = useSearchParams()
  const outcome = mintOutcome(params.get('google'))
  // Collapsed by DEFAULT. These rows are correct and need nothing; shown as a
  // list of empty inputs they read as forty-five things to type.
  const [showVault, setShowVault] = useState(false)

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

  const reload = () => {
    getAgentCredentialStatus(agent.slug)
      .then(setRows)
      .catch(() => {})
  }

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
    return (
      <div className="max-w-4xl px-6 py-8">
        <WorkbenchSubHeader title="Credentials" />
        <WorkbenchSkeleton />
      </div>
    )
  }

  const sec = sections(rows)

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
        <span className="text-[11px] text-foreground-subtle">
          {r.source}
          {r.updated_at ? ` · ${new Date(r.updated_at).toLocaleDateString()}` : ''}
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
        className="ml-auto w-56 rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground placeholder:text-muted-foreground"
      />
      <button
        type="button"
        onClick={() => void save(r.name)}
        disabled={busy || !(draft[r.name] ?? '').trim()}
        className="rounded-md bg-primary px-2 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40"
      >
        Save
      </button>
      {r.set && (
        <button
          type="button"
          onClick={() => void remove(r.name)}
          disabled={busy}
          aria-label={`Remove ${r.name}`}
          className="px-1 text-muted-foreground hover:text-destructive disabled:opacity-40"
        >
          ✕
        </button>
      )}
    </div>
  )

  return (
    <div className="max-w-4xl px-6 py-8" data-testid="agent-credentials">
      <WorkbenchSubHeader title="Credentials" count={rows.length} />

      {outcome && (
        <p
          className={`mb-3 text-[13px] ${outcome.tone === 'ok' ? 'text-success' : 'text-destructive'}`}
          data-testid="google-mint-outcome"
        >
          {outcome.text}
        </p>
      )}

      {rows.length === 0 ? (
        // Zero refs is UNDECLARED, not provisioned — the state every agent is in
        // before someone writes a runtime.yaml. Saying "ready" would assert that
        // a box can run it, which nobody has established.
        <p className="text-[13px] text-muted-foreground" data-testid="agent-credentials-undeclared">
          This agent declares no secrets. Shape lives in its repo’s{' '}
          <code className="font-mono text-[12px]">runtime.yaml</code> and reaches canopy-web as the
          registry’s secret refs — until it declares some, there is nothing to provision here.
        </p>
      ) : (
        <>
          {/* The lede: whether this screen wants anything from the reader. */}
          <p className="mb-4 text-[13px] text-muted-foreground" data-testid="agent-credentials-summary">
            {headline(rows)}
          </p>

          <AgentVaultSection slug={agent.slug} onImported={reload} />

          {declaresMailbox(rows) && (
            <section className="mb-5" data-testid="needs-you">
              <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                Needs a person
              </h3>
              <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-card px-3 py-2">
                <div className="text-[13px] text-foreground">
                  Google mailbox
                  <span className="ml-2 text-[12px] text-muted-foreground">
                    a token can only be minted by signing in — nothing else here can do it for you
                  </span>
                </div>
                <button
                  type="button"
                  onClick={() => void connectMailbox()}
                  disabled={busy}
                  className="ml-auto rounded-md bg-primary px-2 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40"
                >
                  Connect Google mailbox
                </button>
              </div>
            </section>
          )}

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
                className="text-[12px] text-muted-foreground underline-offset-2 hover:underline"
                data-testid="toggle-vault-refs"
                aria-expanded={showVault}
              >
                {showVault ? '▾' : '▸'} {sec.fromVault.length} resolve from 1Password on the
                box {showVault ? '' : '— nothing to do'}
              </button>
              {showVault && (
                <>
                  <p className="mt-2 text-[12px] text-muted-foreground">
                    The runner reads these with its 1Password service-account token. Storing one here
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

      <p className="mt-4 text-[11px] text-foreground-subtle">
        Values are write-only: encrypted at rest, and readable only by a runner this agent routes to.
        This page can show whether a secret is set, never what is in it.
      </p>
    </div>
  )
}
