import { useEffect, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import { runnerHref } from '@/lib/scopedLinks'
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

/** How often the page re-reads an approved invitation until the box pairs. */
const PAIR_POLL_MS = 5000

/**
 * Approve a new cloud runner — the link `up.sh` prints and the invitation email
 * carries (`/w/:workspace/runner-invitations/:invitationId`).
 *
 * Shows what is being let in (name, workspace, instance role, who asked, when it
 * runs out — and, for a machine running several runners, every runner it pairs)
 * and one Approve button: one approval pairs every runner. Once approved it says so, re-reads
 * the invitation until the box pairs, then says that and links to the runner. The passkey step-up is the API client's: a
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

  // Approved but not yet paired: the box asks every 15s, so keep re-reading
  // until it has (`consumed`) or the invitation runs out.
  const waitingForBox = inv?.status === 'approved'
  useEffect(() => {
    if (!invitationId || !waitingForBox) return
    const timer = setInterval(() => {
      getRunnerInvitation(invitationId).then(setInv).catch(() => {})
    }, PAIR_POLL_MS)
    return () => clearInterval(timer)
  }, [invitationId, waitingForBox])

  if (!invitationId) return null
  if (loadError) {
    return (
      <div className="max-w-2xl">
        <p className="text-sm text-destructive">{loadError}</p>
      </div>
    )
  }
  if (!inv) return <div className="h-32 max-w-2xl animate-pulse rounded-lg bg-muted" />
  const runners = inv.runners ?? []
  // A FLEET's invitation (`apps/harness/fleets.py`): approving it lets every
  // instance of one Auto Scaling group join, and is used up by the approval.
  const fleet = inv.fleet ?? null
  const pairedRunnerId = inv.runner_id ?? runners.find((r) => r.runner_id)?.runner_id ?? null

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
        <h1 className="text-sm font-semibold text-foreground">
          {fleet ? 'Approve a runner fleet' : 'Approve a new cloud runner'}
        </h1>
        <dl className="mt-3 grid gap-1 text-[13px] text-foreground-secondary sm:grid-cols-[8rem_1fr]">
          <dt className="font-medium text-foreground">{fleet ? 'Fleet' : 'Runner'}</dt>
          <dd className="m-0 font-mono">{inv.name}</dd>
          {fleet && (
            <>
              <dt className="font-medium text-foreground">Group</dt>
              <dd className="m-0 break-all font-mono">
                {fleet.group_name} ({fleet.region})
              </dd>
              <dt className="font-medium text-foreground">Members</dt>
              <dd className="m-0" data-testid="invitation-fleet-size">
                {fleet.min_members}–{fleet.max_members}, {fleet.member_turns} turns each
              </dd>
            </>
          )}
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
            {inv.status}: {fleet && inv.status === 'consumed' ? 'Approved: the fleet is active.' : STATUS_TEXT[inv.status]}
          </dd>
        </dl>
        {inv.directed && (
          <p className="mt-3 text-[12px] text-foreground" data-testid="invitation-directed">
            A test box: it pairs directed only — it takes only turns pinned to it and chats started on it, no routed
            work. Its runner page can turn that off.
          </p>
        )}
        {fleet ? (
          <p className="mt-3 text-[12px] text-muted-foreground" data-testid="invitation-fleet">
            Approving is the only approval this fleet ever needs: from then on every instance AWS says belongs to its
            group joins as your runner in {inv.workspace}, signing in as that instance, and canopy-web keeps between{' '}
            {fleet.min_members} and {fleet.max_members} of them running by the work waiting. They share one credential
            set and one key for reading zero-knowledge secrets.
          </p>
        ) : runners.length > 0 ? (
          <>
            <table className="mt-4 w-full text-left text-[13px]" aria-label="Runners">
              <thead className="text-[12px] text-muted-foreground">
                <tr>
                  <th className="py-1 pr-3 font-medium">Runner</th>
                  <th className="py-1 pr-3 font-medium">Serves</th>
                  <th className="py-1 pr-3 font-medium">Workspace</th>
                  <th className="py-1 font-medium">Paired</th>
                </tr>
              </thead>
              <tbody>
                {runners.map((r) => (
                  <tr key={r.name} className="border-t border-border" data-testid="invitation-runner">
                    <td className="py-1 pr-3 font-mono">{r.name}</td>
                    <td className="py-1 pr-3">{r.kind === 'agent' ? `only agent ${r.slug}` : 'its workspace tree'}</td>
                    <td className="py-1 pr-3">{r.workspace}</td>
                    <td className="py-1 font-mono text-[12px]">{r.runner_id ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-3 text-[12px] text-muted-foreground">
              Approving makes each of these {runners.length} runners yours in its workspace: each is its own user on
              the machine with its own key, holds no token, and serves nothing until you route work to it
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

      {inv.status === 'approved' && (
        <p role="status" className="rounded-md border border-border bg-muted px-3 py-2 text-sm text-foreground">
          Approved. Waiting for the box to pair; this page updates when it does.
        </p>
      )}
      {inv.status === 'consumed' && fleet && (
        <p role="status" className="rounded-md border border-border bg-muted px-3 py-2 text-sm text-foreground">
          Fleet {inv.name} is active. Once its first member has joined, open that member&apos;s page and give the fleet
          its Claude credentials: every member reads them.
        </p>
      )}
      {inv.status === 'consumed' && !fleet && (
        <p role="status" className="rounded-md border border-border bg-muted px-3 py-2 text-sm text-foreground">
          Paired: {inv.name} is your runner now.
          {pairedRunnerId && (
            <>
              {' '}
              <Link to={runnerHref({ id: pairedRunnerId, workspace: inv.workspace })} className="text-primary hover:underline">
                Open its page
              </Link>{' '}
              to give it its Claude credentials.
            </>
          )}
        </p>
      )}

      {actionError && <p className="text-sm text-destructive">{actionError}</p>}

      {inv.status === 'pending' && (
        <Button type="button" size="sm" disabled={busy} onClick={() => void approve()}>
          {busy ? 'Approving…' : 'Approve'}
        </Button>
      )}
    </div>
  )
}
