import { useEffect, useState } from 'react'

import {
  type AgentSalesforce,
  checkAgentSalesforce,
  deleteAgentSalesforce,
  getAgentSalesforce,
  setAgentSalesforce,
} from '@/api/agents'
import { useAuth } from '@/auth/AuthProvider'
import type { CredStatus } from '@/pages/agents/agentCredentials'

// Salesforce is a DELEGATED identity: chrome-sales acts as one Salesforce user
// (Eva's), and other agents BORROW it rather than having accounts of their own.
// The loan names the lending agent; the credential stays the lender's own, held
// once, so a re-mint reaches every borrower and nothing is copied. Lending needs
// the owner of both agents and is checked against Salesforce before it is saved.
// This section answers both directions: whose identity this agent borrows, and
// who borrows its own. See the Salesforce section of apps/agents/delegations.py.

const TONE = {
  destructive: 'bg-destructive/10 text-destructive border-destructive/30',
  muted: 'bg-muted text-muted-foreground border-border',
  success: 'bg-success/10 text-success border-success/30',
} as const

function day(iso?: string | null): string {
  return iso ? new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : ''
}

export function AgentSalesforceSection({ slug, onStatus }: { slug: string; onStatus?: (s: CredStatus) => void }) {
  const { user } = useAuth()
  const [sf, setSf] = useState<AgentSalesforce | null>(null)
  const [lender, setLender] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let off = false
    getAgentSalesforce(slug)
      .then((s) => !off && setSf(s))
      .catch(() => {})
    return () => {
      off = true
    }
  }, [slug])

  useEffect(() => {
    if (!sf) return
    onStatus?.(
      !sf.set
        ? { label: 'Borrows none', tone: 'muted' }
        : sf.error
          ? { label: 'Not working', tone: 'destructive' }
          : { label: 'Working', tone: 'success' },
    )
  }, [sf, onStatus])

  const run = async (fn: () => Promise<AgentSalesforce>) => {
    setBusy(true)
    setError(null)
    try {
      setSf(await fn())
      setLender('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  if (!sf) return null
  const isOwner = Boolean(user?.email && sf.owner_email && user.email === sf.owner_email)
  const lentTo = sf.lent_to ?? []
  const tone = sf.set ? (sf.error ? 'destructive' : 'success') : 'muted'

  return (
    <section id="cred-salesforce" className="mb-5 scroll-mt-6" data-testid="agent-salesforce">
      <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        Salesforce — delegated identity
      </h3>
      <p className="mb-2 max-w-2xl text-[11px] text-muted-foreground">
        Agents act in Salesforce (the chrome-sales tools) only as an identity another agent lends them. They have no
        Salesforce accounts of their own, and never use a person&rsquo;s.
      </p>
      <div className="rounded-lg border border-border bg-card px-3 py-2 text-[12px]">
        <div className="flex flex-wrap items-center gap-2" data-testid="salesforce-status">
          <span className={`rounded border px-1.5 py-0.5 text-[11px] ${TONE[tone]}`}>
            {!sf.set ? 'Borrows none' : sf.error ? 'Not working' : 'Working'}
          </span>
          {sf.set && (
            <span className="text-foreground-secondary">
              acts as <span className="font-mono">{sf.username || '?'}</span>, lent by{' '}
              <span className="font-mono">{sf.lender}</span>
              {sf.checked_at ? <> · checked {day(sf.checked_at)}</> : null}
            </span>
          )}
          {sf.set && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void run(() => checkAgentSalesforce(slug))}
              className="ml-auto rounded-md border border-input px-2 py-0.5 text-[11px] text-foreground disabled:opacity-40"
            >
              Check now
            </button>
          )}
        </div>
        {sf.error && <p className="mt-1 text-destructive">{sf.error}</p>}
        {lentTo.length > 0 && (
          <p className="mt-1 text-[11px] text-muted-foreground" data-testid="salesforce-lent-to">
            Lends its own Salesforce identity to{' '}
            {lentTo.map((a, i) => (
              <span key={a}>
                {i > 0 ? ', ' : null}
                <span className="font-mono">{a}</span>
              </span>
            ))}
            .
          </p>
        )}

        {isOwner ? (
          <div className="mt-3 border-t border-border pt-2" data-testid="salesforce-setup">
            <div className="flex flex-wrap items-center gap-2">
              <input
                type="text"
                autoComplete="off"
                aria-label="Lending agent"
                value={lender}
                onChange={(e) => setLender(e.target.value)}
                placeholder={sf.set ? `lent by ${sf.lender} — name another to replace` : 'lending agent, e.g. eva'}
                className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground sm:min-h-0 sm:w-72"
              />
              <button
                type="button"
                disabled={busy || !lender.trim()}
                onClick={() => void run(() => setAgentSalesforce(slug, lender.trim()))}
                className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40 sm:min-h-0"
              >
                {busy ? 'Checking…' : 'Lend'}
              </button>
              {sf.set && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void run(() => deleteAgentSalesforce(slug))}
                  className="min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground disabled:opacity-40 sm:min-h-0"
                >
                  Withdraw
                </button>
              )}
            </div>
            <p className="mt-1 text-[11px] text-muted-foreground">
              You must own the lending agent too. Checked against Salesforce before it is saved; the credential is
              never copied or shown.
            </p>
          </div>
        ) : (
          !sf.set && (
            <p className="mt-2 text-[11px] text-muted-foreground">
              Only the agent&rsquo;s owner{sf.owner_email ? <> ({sf.owner_email})</> : null} can lend it a Salesforce
              identity.
            </p>
          )
        )}
        {error && <p className="mt-2 text-destructive" role="alert">{error}</p>}
      </div>
    </section>
  )
}
