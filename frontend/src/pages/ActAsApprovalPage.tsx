import { useEffect, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import {
  approveActAsRequest,
  declineActAsRequest,
  getActAsRequest,
  offeredScopes,
  statusLine,
  stopActAsRequest,
  type ActAsRequest,
} from '@/api/actAs'
import { openInBrowser, PASSKEY_IN_BROWSER, passkeysWorkHere, useWhileAway } from '@/auth/passkeyHere'

/**
 * Approve an agent acting as you on canopy (`/act-as/:requestId`) — the link the push
 * and the supervisor feed carry. Shows which agent asks, its reason verbatim, the
 * conversation it is working in and what it asked for; approving takes a passkey touch
 * made for this request, and lasts 30 minutes from that touch while the turn runs.
 * In the Canopy Runner app, where no passkey can be used, the decision is made on
 * this same page in the person's browser, and this one follows it (`passkeyHere.ts`).
 */
export function ActAsApprovalPage(): JSX.Element | null {
  const { requestId } = useParams()
  const [req, setReq] = useState<ActAsRequest | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [inBrowser, setInBrowser] = useState(false)
  useWhileAway(
    () => requestId && getActAsRequest(requestId).then(setReq).catch(() => undefined),
    inBrowser && req?.status === 'pending',
  )

  useEffect(() => {
    if (!requestId) return
    let cancelled = false
    getActAsRequest(requestId)
      .then((r) => !cancelled && setReq(r))
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load the request')
      })
    return () => {
      cancelled = true
    }
  }, [requestId])

  if (!requestId) return null
  if (error && !req) return <p className="max-w-2xl text-sm text-destructive">{error}</p>
  if (!req) return <div className="h-32 max-w-2xl animate-pulse rounded-lg bg-muted" />

  async function act(fn: () => Promise<ActAsRequest>) {
    setBusy(true)
    setError(null)
    try {
      setReq(await fn())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  const agent = req.agent_name || req.agent
  return (
    <div className="max-w-2xl space-y-4">
      <section className="rounded-lg border border-border bg-card p-5" aria-label="The request">
        <h1 className="text-sm font-semibold text-foreground">{agent} asks to act as you on canopy</h1>
        <blockquote
          className="mt-3 border-l-2 border-border pl-3 text-[13px] text-foreground whitespace-pre-wrap"
          data-testid="act-as-reason"
        >
          {req.reason}
        </blockquote>
        <dl className="mt-3 grid gap-1 text-[13px] text-foreground-secondary sm:grid-cols-[8rem_1fr]">
          <dt className="font-medium text-foreground">Agent</dt>
          <dd className="m-0">{agent}</dd>
          <dt className="font-medium text-foreground">Working in</dt>
          <dd className="m-0">
            {req.session_url ? (
              <Link to={req.session_url} className="text-primary hover:underline">
                its conversation
              </Link>
            ) : (
              <span className="font-mono">turn {req.turn_id.slice(0, 8)}</span>
            )}
          </dd>
          <dt className="font-medium text-foreground">Asks for</dt>
          <dd className="m-0" data-testid="act-as-requested">
            {req.requested_scope === 'read'
              ? 'read-only'
              : req.requested_scope === 'write'
                ? 'read + write'
                : 'read-only or read + write (your choice)'}
          </dd>
          <dt className="font-medium text-foreground">Status</dt>
          <dd className="m-0" data-testid="act-as-status">
            {statusLine(req, agent)}
          </dd>
        </dl>
        {(req.status === 'pending' || req.status === 'approved') && <p className="mt-3 text-[12px] text-muted-foreground">
          Approving lets {agent} use canopy as you — within your own access, for 30 minutes from your passkey
          touch, and only while this turn runs. It can never touch your tokens, passkeys, vaults, memory
          settings or these permissions. You can stop it any time.
        </p>}
        {error && <p className="mt-2 text-sm text-destructive">{error}</p>}
        {req.status === 'pending' && !passkeysWorkHere() && (
          <div className="mt-4 space-y-2" data-testid="act-as-in-browser">
            <p className="text-[12px] text-muted-foreground">
              {inBrowser ? 'Waiting for you to decide in your browser…' : PASSKEY_IN_BROWSER}
            </p>
            <Button
              variant={inBrowser ? 'outline' : 'default'}
              onClick={() => {
                openInBrowser(`/act-as/${req.id}`)
                setInBrowser(true)
              }}
            >
              {inBrowser ? 'Open it again' : 'Decide in your browser'}
            </Button>
          </div>
        )}
        {req.status === 'pending' && passkeysWorkHere() && (
          <div className="mt-4 flex flex-wrap gap-2">
            {offeredScopes(req.requested_scope ?? 'either').map((scope, i) => (
              <Button
                key={scope}
                variant={i === 0 ? 'default' : 'outline'}
                disabled={busy}
                data-testid={`act-as-allow-${scope}`}
                onClick={() => void act(() => approveActAsRequest(req.id, scope))}
              >
                {scope === 'read' ? 'Allow read-only (30 min)' : 'Allow read + write (30 min)'}
              </Button>
            ))}
            <Button
              variant="ghost"
              disabled={busy}
              data-testid="act-as-decline"
              onClick={() => void act(() => declineActAsRequest(req.id))}
            >
              Decline
            </Button>
          </div>
        )}
        {req.status === 'approved' && (
          <div className="mt-4">
            <Button
              variant="outline"
              disabled={busy}
              data-testid="act-as-stop"
              onClick={() => void act(() => stopActAsRequest(req.id))}
            >
              Stop now
            </Button>
          </div>
        )}
      </section>
    </div>
  )
}
