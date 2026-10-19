import { useEffect, useState, type JSX } from 'react'
import { useParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import {
  approveRunnerPairing,
  declineRunnerPairing,
  getRunnerPairing,
  RunnerPairingError,
  type RunnerPairingOut,
} from '@/api/runnerPairings'

const STATUS_TEXT: Record<RunnerPairingOut['status'], string> = {
  pending: 'Waiting for your approval.',
  approved: 'Approved. If Canopy Runner did not finish pairing, approve again to send it a new code.',
  consumed: 'Done: this computer is paired.',
  declined: 'Declined. Canopy Runner will not be paired; start again from it if that was a mistake.',
  expired: 'Expired. Start pairing again from Canopy Runner.',
}

const PLATFORM: Record<string, string> = { macos: 'macOS', windows: 'Windows', linux: 'Linux' }
const KEY_KIND: Record<string, string> = {
  'secure-enclave': 'Secure Enclave (held by the hardware)',
  tpm: 'TPM (held by the hardware)',
  software: 'Software key (a file on the computer)',
}

/**
 * /pair/:code — approve a computer that is asking to become your runner.
 *
 * Canopy Runner, just installed, opens this page in the person's browser with the
 * code it shows in its own window. Root, not under /w/: the pairing has no
 * workspace until the person picks one here. Approving needs a passkey (the API
 * client answers 403 `step_up_required` by asking for it and retrying), and its
 * answer is a redirect to the computer's own loopback address carrying a
 * one-time secret — so it only works in a browser on that computer.
 */
export function PairRunnerPage(): JSX.Element | null {
  const { code } = useParams()
  const [pairing, setPairing] = useState<RunnerPairingOut | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [workspace, setWorkspace] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  useEffect(() => {
    if (!code) return
    let cancelled = false
    getRunnerPairing(code)
      .then((p) => {
        if (cancelled) return
        setPairing(p)
        setName(p.suggested_name)
        setWorkspace(p.workspaces[0]?.slug ?? '')
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setLoadError(
          e instanceof RunnerPairingError && e.status === 404
            ? 'No pairing has this code. Check it against the one Canopy Runner shows, or start again from it.'
            : e instanceof Error ? e.message : 'Failed to load the pairing',
        )
      })
    return () => {
      cancelled = true
    }
  }, [code])

  if (!code) return null
  if (loadError) {
    return (
      <div className="mx-auto max-w-2xl px-6 py-8">
        <p className="text-sm text-destructive">{loadError}</p>
      </div>
    )
  }
  if (!pairing) return <div className="mx-auto mt-8 h-32 max-w-2xl animate-pulse rounded-lg bg-muted" />

  const open = pairing.status === 'pending' || pairing.status === 'approved'
  const noWorkspace = pairing.workspaces.length === 0

  async function approve() {
    if (!code) return
    setBusy(true)
    setActionError(null)
    try {
      const res = await approveRunnerPairing(code, { workspace, name: name.trim() })
      // The computer's own loopback takes the one-time secret and finishes pairing.
      window.location.assign(res.redirect_url)
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Something went wrong')
      setBusy(false)
    }
  }

  async function decline() {
    if (!code || !pairing) return
    setBusy(true)
    setActionError(null)
    try {
      await declineRunnerPairing(code)
      setPairing({ ...pairing, status: 'declined' })
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-4 px-6 py-8">
      <section className="rounded-lg border border-border bg-card p-5" aria-label="The pairing">
        <h1 className="text-sm font-semibold text-foreground">Pair this computer as your runner</h1>
        <p className="mt-3 font-mono text-2xl font-semibold tracking-widest text-foreground" data-testid="pairing-code">
          {pairing.user_code}
        </p>
        <p className="mt-1 text-[13px] text-foreground-secondary">
          Check it matches the code Canopy Runner shows on your computer. If it does not, or you did not just install
          Canopy Runner, decline.
        </p>
        <dl className="mt-3 grid gap-1 text-[13px] text-foreground-secondary sm:grid-cols-[8rem_1fr]">
          <dt className="font-medium text-foreground">Computer</dt>
          <dd className="m-0 font-mono">{pairing.hostname || '—'}</dd>
          <dt className="font-medium text-foreground">Account</dt>
          <dd className="m-0 font-mono">{pairing.account || '—'}</dd>
          <dt className="font-medium text-foreground">System</dt>
          <dd className="m-0">{PLATFORM[pairing.platform] ?? pairing.platform}</dd>
          <dt className="font-medium text-foreground">Key</dt>
          <dd className="m-0">{KEY_KIND[pairing.kind] ?? pairing.kind}</dd>
          <dt className="font-medium text-foreground">Expires</dt>
          <dd className="m-0">{new Date(pairing.expires_at).toLocaleString()}</dd>
          <dt className="font-medium text-foreground">Status</dt>
          <dd className="m-0" data-testid="pairing-status">
            {STATUS_TEXT[pairing.status]}
          </dd>
        </dl>
      </section>

      {open && (
        <section className="space-y-3 rounded-lg border border-border bg-card p-5" aria-label="Approve">
          {noWorkspace ? (
            <p className="m-0 text-[13px] text-muted-foreground">
              You cannot run work in any workspace, so you cannot pair a runner. Ask a workspace admin to make you an
              editor.
            </p>
          ) : (
            <>
              <label className="flex flex-col gap-1 text-[13px]">
                <span className="font-medium text-foreground">Workspace</span>
                <select
                  value={workspace}
                  onChange={(e) => setWorkspace(e.target.value)}
                  className="min-h-9 rounded-md border border-input bg-input px-2 text-foreground"
                >
                  {pairing.workspaces.map((w) => (
                    <option key={w.slug} value={w.slug}>
                      {w.name === w.slug ? w.slug : `${w.name} (${w.slug})`}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1 text-[13px]">
                <span className="font-medium text-foreground">Runner name</span>
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  maxLength={63}
                  spellCheck={false}
                  className="min-h-9 rounded-md border border-input bg-input px-2 font-mono text-foreground"
                />
                <span className="text-[12px] text-muted-foreground">
                  Lowercase letters, digits and dashes, starting with a letter.
                </span>
              </label>
              <p className="m-0 text-[12px] text-muted-foreground">
                Approving makes this computer your runner in that workspace. It signs in with its own key, holds no
                token of yours, and serves nothing until you route work to it. You will be asked for your passkey.
              </p>
            </>
          )}
          {actionError && <p className="m-0 text-sm text-destructive">{actionError}</p>}
          <div className="flex gap-2">
            {!noWorkspace && (
              <Button type="button" size="sm" disabled={busy || !workspace || !name.trim()} onClick={() => void approve()}>
                {busy ? 'Approving…' : 'Approve'}
              </Button>
            )}
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void decline()}>
              Decline
            </Button>
          </div>
        </section>
      )}
    </div>
  )
}
