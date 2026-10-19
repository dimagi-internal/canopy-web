import { useEffect, useState, type JSX } from 'react'
import { useParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import {
  approveRunnerInvitation,
  getRunnerInvitation,
  RunnerInvitationError,
  type RunnerInvitationOut,
} from '@/api/runnerInvitations'

const STATUS_TEXT: Record<RunnerInvitationOut['status'], string> = {
  pending: 'Waiting for your approval.',
  approved: 'Approved. The box pairs the next time it asks, within a minute.',
  consumed: 'Used: the box has paired.',
  expired: 'Expired. Run up.sh again for a new invitation.',
}

/**
 * Approve a new cloud runner — the link `up.sh` prints and the invitation email
 * carries (`/w/:workspace/runner-invitations/:invitationId`).
 *
 * Shows what is being let in (name, workspace, instance role, who asked, when it
 * runs out — and, for a box of isolation units, every unit it pairs) and one
 * Approve button: one approval pairs every unit. The passkey step-up is the API client's: a
 * 403 `step_up_required` prompts for the passkey and retries, so this page has
 * no passkey code of its own. Only the invitation's creator can open it.
 */
export function RunnerInvitationApprovePage(): JSX.Element | null {
  const { invitationId } = useParams()
  const [inv, setInv] = useState<RunnerInvitationOut | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  useEffect(() => {
    if (!invitationId) return
    let cancelled = false
    getRunnerInvitation(invitationId)
      .then((r) => {
        if (!cancelled) setInv(r)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setLoadError(
          e instanceof RunnerInvitationError && e.status === 404
            ? 'No such invitation. Only the person who created it can open it.'
            : e instanceof Error ? e.message : 'Failed to load the invitation',
        )
      })
    return () => {
      cancelled = true
    }
  }, [invitationId])

  if (!invitationId) return null
  if (loadError) {
    return (
      <div className="max-w-2xl">
        <p className="text-sm text-destructive">{loadError}</p>
      </div>
    )
  }
  if (!inv) return <div className="h-32 max-w-2xl animate-pulse rounded-lg bg-muted" />
  const units = inv.units ?? []

  async function approve() {
    if (!inv) return
    setBusy(true)
    setActionError(null)
    try {
      setInv(await approveRunnerInvitation(inv.id))
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="max-w-2xl space-y-4">
      <section className="rounded-lg border border-border bg-card p-5" aria-label="The invitation">
        <h1 className="text-sm font-semibold text-foreground">Approve a new cloud runner</h1>
        <dl className="mt-3 grid gap-1 text-[13px] text-foreground-secondary sm:grid-cols-[8rem_1fr]">
          <dt className="font-medium text-foreground">Runner</dt>
          <dd className="m-0 font-mono">{inv.name}</dd>
          <dt className="font-medium text-foreground">Workspace</dt>
          <dd className="m-0">{inv.workspace}</dd>
          <dt className="font-medium text-foreground">Instance role</dt>
          <dd className="m-0 break-all font-mono">{inv.role_arn}</dd>
          <dt className="font-medium text-foreground">Asked by</dt>
          <dd className="m-0">{inv.created_by_email}</dd>
          <dt className="font-medium text-foreground">Asked</dt>
          <dd className="m-0">{new Date(inv.created_at).toLocaleString()}</dd>
          <dt className="font-medium text-foreground">Expires</dt>
          <dd className="m-0">{new Date(inv.expires_at).toLocaleString()}</dd>
          <dt className="font-medium text-foreground">Status</dt>
          <dd className="m-0" data-testid="invitation-status">
            {inv.status}: {STATUS_TEXT[inv.status]}
          </dd>
        </dl>
        {units.length > 0 ? (
          <>
            <table className="mt-4 w-full text-left text-[13px]" aria-label="Units">
              <thead className="text-[12px] text-muted-foreground">
                <tr>
                  <th className="py-1 pr-3 font-medium">Unit runner</th>
                  <th className="py-1 pr-3 font-medium">Serves</th>
                  <th className="py-1 pr-3 font-medium">Workspace</th>
                  <th className="py-1 font-medium">Paired</th>
                </tr>
              </thead>
              <tbody>
                {units.map((u) => (
                  <tr key={u.name} className="border-t border-border" data-testid="invitation-unit">
                    <td className="py-1 pr-3 font-mono">{u.name}</td>
                    <td className="py-1 pr-3">{u.kind === 'agent' ? `only agent ${u.slug}` : 'its workspace tree'}</td>
                    <td className="py-1 pr-3">{u.workspace}</td>
                    <td className="py-1 font-mono text-[12px]">{u.runner_id ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-3 text-[12px] text-muted-foreground">
              Approving makes each of these {units.length} units your runner in its workspace: each is its own user on
              the box with its own key, holds no token, and serves nothing until you route work to it
              {inv.has_ecdh_key ? '. Their keys for reading zero-knowledge secrets are approved with them' : ''}.
            </p>
          </>
        ) : (
          <p className="mt-3 text-[12px] text-muted-foreground">
            Approving makes this box your runner in {inv.workspace}. It signs in with its instance role, holds no
            token, and serves nothing until you route work to it
            {inv.has_ecdh_key ? '. Its key for reading zero-knowledge secrets is approved with it' : ''}.
          </p>
        )}
      </section>

      {actionError && <p className="text-sm text-destructive">{actionError}</p>}

      {inv.status === 'pending' && (
        <Button type="button" size="sm" disabled={busy} onClick={() => void approve()}>
          {busy ? 'Approving…' : 'Approve'}
        </Button>
      )}
    </div>
  )
}
