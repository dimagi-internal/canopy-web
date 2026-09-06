import { useEffect, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import {
  deleteAgentCredential,
  getAgentCredentialStatus,
  setAgentCredentials,
  type AgentCredentialStatusOut,
} from '@/api/agents'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { groupRefs, summarize } from '@/pages/agents/agentCredentials'
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

  const s = summarize(rows)
  const ordered = groupRefs(rows)

  return (
    <div className="max-w-4xl px-6 py-8" data-testid="agent-credentials">
      <WorkbenchSubHeader title="Credentials" count={rows.length} />

      {s.undeclared ? (
        // Zero refs is UNDECLARED, not provisioned — and it is the state every
        // agent is in before someone writes a runtime.yaml. Saying "ready" here
        // would assert a box can run it, which nobody has established.
        <p className="text-[13px] text-muted-foreground" data-testid="agent-credentials-undeclared">
          This agent declares no secrets. Shape lives in its repo’s{' '}
          <code className="font-mono text-[12px]">runtime.yaml</code> and reaches canopy-web as the
          registry’s secret refs — until it declares some, there is nothing to provision here.
        </p>
      ) : (
        <>
          {/* Reports; does not judge. canopy-web holds only the secrets that need
              to be here — the rest live in 1Password and are resolved on the box
              with the runner's service-account token. Framing those as missing
              made this page report "45 blockers, this agent cannot run" about a
              healthy ACE, which teaches people to ignore it; the one genuinely
              dead credential then hides among the false alarms. Whether a secret
              WORKS is a question only the box can answer — a readiness drill is
              what answers it. */}
          <p className="mb-3 text-[13px] text-muted-foreground" data-testid="agent-credentials-summary">
            canopy-web stores {s.storedHere.length} of {s.declaredCount} declared secrets.
            {s.fromVault.length > 0 && (
              <> The other {s.fromVault.length} resolve from 1Password on the box, using the
              runner’s service-account token.</>
            )}
          </p>

          <div className="space-y-2">
            {ordered.map((r) => (
              <div
                key={r.name}
                className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card px-3 py-2"
                data-testid={`cred-${r.name}`}
              >
                <span className={r.set ? 'text-success' : 'text-muted-foreground'}>
                  {r.set ? '●' : '○'}
                </span>
                <span className="font-mono text-[12px] text-foreground">{r.name}</span>

                {!r.declared && (
                  // An orphan: stored, but nothing declares it any more. A live
                  // secret nothing accounts for — hiding it is how it stays that way.
                  <span className="rounded border border-border px-1 text-[10px] uppercase text-muted-foreground">
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
                  placeholder={r.set ? 'rotate…' : 'paste to set'}
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
            ))}
          </div>
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
