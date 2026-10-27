import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { toneClasses } from 'canopy-ui/lib'

import { getAgentCredentialStatus, startGoogleMint, type AgentCredentialStatusOut } from '@/api/agents'
import { getAgentMailbox, type AgentMailboxOut } from '@/api/mail'
import type { CredStatus } from '@/pages/agents/agentCredentials'
import { AgentGitHubSection } from '@/pages/agents/AgentGitHubSection'
import { declaresMailbox, GOG_TOKEN_REF, mintOutcome } from '@/pages/agents/googleMint'

// The agent's External accounts tab: the outside accounts it acts as —
// GitHub and its Google mailbox.
//
// It LEADS with a status list (#1314): one line per account, each reported up
// by its own detail section, so the one that needs a person (a mailbox nobody
// connected) is the first thing on screen, with its action on the same line.

const TONE: Record<CredStatus['tone'], string> = {
  success: toneClasses('success'),
  warning: toneClasses('warning'),
  destructive: toneClasses('destructive'),
  muted: toneClasses('neutral'),
}

const MARK: Record<CredStatus['tone'], string> = { success: '✓', warning: '!', destructive: '✗', muted: '–' }

type StatusKey = 'github'

export function AgentAccountsPanel({
  agent,
}: {
  agent: { slug: string; email?: string | null }
}) {
  const [rows, setRows] = useState<AgentCredentialStatusOut[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [params] = useSearchParams()
  const outcome = mintOutcome(params.get('google'), { account: params.get('account'), agentEmail: params.get('expected') || agent.email })
  const [mailBox, setMailBox] = useState<AgentMailboxOut | null>(null)
  const [status, setStatus] = useState<Partial<Record<StatusKey, CredStatus>>>({})
  const report = useCallback(
    (key: StatusKey) => (s: CredStatus) => setStatus((prev) => ({ ...prev, [key]: s })),
    [],
  )
  // Stable per key, so a section's effect does not re-fire on every render.
  const [onGitHub] = useState(() => report('github'))

  useEffect(() => {
    getAgentCredentialStatus(agent.slug).then(setRows, () => setRows([]))
  }, [agent.slug])
  useEffect(() => {
    if (!agent.email?.trim()) return
    getAgentMailbox(agent.slug).then(setMailBox).catch(() => undefined)
  }, [agent.slug, agent.email])

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

  // A canopy-hosted address has no Google mailbox to connect (apps/mail): it must not
  // imply a gog-token, nor read "Not connected" for something that needs nothing.
  const hosted = mailBox?.kind === 'hosted'
  const hasMailbox = rows !== null && declaresMailbox(rows, hosted ? null : agent.email)
  const gog = rows?.find((r) => r.name === GOG_TOKEN_REF)
  const mailbox: CredStatus = gog?.set
    ? { label: 'Connected', tone: 'success' }
    : gog
      ? { label: 'From 1Password', tone: 'muted' }
      : { label: 'Not connected', tone: 'destructive' }

  const statusRows: { key: string; name: string; href?: string; s: CredStatus | undefined }[] = [
    { key: 'github', name: 'GitHub', href: '#cred-github', s: status.github },
    ...(hosted ? [{ key: 'email', name: 'Email', href: '#reach', s: mailBox?.live
      ? { label: 'Canopy-hosted', tone: 'success' } as CredStatus
      : { label: 'Canopy-hosted, not routed', tone: 'warning' } as CredStatus }] : []),
    ...(hasMailbox ? [{ key: 'mailbox', name: hosted ? 'Google sign-in (Drive)' : 'Google mailbox', s: mailbox }] : []),
  ]

  return (
    <div data-testid="agent-accounts">
      {outcome && (
        <p
          className={`mb-3 text-[13px] ${outcome.tone === 'ok' ? 'text-success' : 'text-destructive'}`}
          data-testid="google-mint-outcome"
        >
          {outcome.text}
          {outcome.link && (
            <>
              {' '}
              <Link to={outcome.link.href} className="font-medium text-primary hover:underline">
                {outcome.link.label}
              </Link>
            </>
          )}
        </p>
      )}

      <table id="cred-mailbox" className="mb-6 w-full scroll-mt-6 text-[12px]" data-testid="credential-status">
        <tbody className="divide-y divide-border">
          {statusRows.map((r) => (
            <tr key={r.key} data-testid={`credential-status-${r.key}`}>
              <td className="w-48 py-1.5 text-foreground">
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
      {error && <p className="mb-3 text-[13px] text-destructive">{error}</p>}

      <AgentGitHubSection slug={agent.slug} onStatus={onGitHub} />
    </div>
  )
}
