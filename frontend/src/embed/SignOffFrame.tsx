import { useEffect, useMemo, useRef, useState, type JSX } from 'react'
import { createCanopyClient, CanopyRestError, type CanopyClient } from 'canopy-client'
import { Button } from 'canopy-ui/ui'

import { credentialToJSON, toRequestOptions } from '../auth/webauthn'
import type { SignOffOut } from '@/api/signOffs'
import { currentFrameBaseUrl } from './frameBase'
import type { HostInit, HostLink } from './hostLink'

const STATE_TEXT: Record<string, string> = {
  approved: 'Already approved.',
  declined: 'Already declined.',
  expired: 'This request has expired. Ask again from the page.',
}

/**
 * One site sign-off, decided on the site's own page (`/embed/sign-off`, framed by
 * `canopy.signOff`): what the site asks, the account it is for, and Approve with
 * passkey / Decline. The passkey assertion is made here, in canopy's frame, against
 * `/api/embed/sign-offs/{id}/approve/begin|finish` with the frame's own token — the
 * site holds that token, so the assertion over canopy's origin is what proves the
 * person (`sign_offs_api.py`). The page is told only how it ended (`sign-off`), never
 * the approval: the site's server reads that from canopy.
 */
export function SignOffFrame({ link, app, signOffId }: { link: HostLink; app: string; signOffId: string }): JSX.Element {
  const [init, setInit] = useState<HostInit | null>(null)
  const [row, setRow] = useState<SignOffOut | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [busy, setBusy] = useState<'approve' | 'decline' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const box = useRef<HTMLDivElement | null>(null)
  const path = `/api/embed/sign-offs/${encodeURIComponent(signOffId)}`

  const client = useMemo<CanopyClient | null>(() => {
    if (!init) return null
    let first = true
    return createCanopyClient({
      baseUrl: currentFrameBaseUrl(),
      fetchToken: async () => {
        // The handshake's token on the first call; the host mints any later one.
        if (first) {
          first = false
          return { token: init.token, expiresAt: init.expiresAt ?? '' }
        }
        return link.requestToken()
      },
      source: app,
    })
  }, [init, link, app])

  useEffect(() => {
    let cancelled = false
    link.waitForInit()
      .then((hostInit) => { if (!cancelled) setInit(hostInit) })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError(`The page could not connect you to canopy (${e instanceof Error ? e.message : 'no token'}).`)
      })
    return () => { cancelled = true }
  }, [link])

  useEffect(() => {
    if (!client) return
    let cancelled = false
    client.rest.json<SignOffOut>(path)
      .then((r) => { if (!cancelled) setRow(r) })
      .catch((e: unknown) => {
        if (cancelled) return
        setLoadError(e instanceof CanopyRestError && e.status === 404
          ? 'There is no request here for you to decide.'
          : 'This request could not be read.')
      })
    return () => { cancelled = true }
  }, [client, path])

  // The host sizes the dialog to what is drawn.
  useEffect(() => {
    const el = box.current
    if (!el) return
    const report = () => link.requestHeight(Math.ceil(el.getBoundingClientRect().height))
    report()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(report)
    observer.observe(el)
    return () => observer.disconnect()
  }, [link, row, loadError, actionError])

  async function approve(): Promise<void> {
    if (!client) return
    setBusy('approve')
    setActionError(null)
    try {
      const begun = await client.rest.json<{ options: Record<string, unknown> }>(`${path}/approve/begin`, { method: 'POST' })
      const credential = (await navigator.credentials.get({
        publicKey: toRequestOptions(begun.options),
      })) as PublicKeyCredential | null
      if (!credential) throw new Error('No passkey was used.')
      const decided = await client.rest.json<SignOffOut>(`${path}/approve/finish`, {
        method: 'POST',
        body: JSON.stringify({ credential: credentialToJSON(credential) }),
      })
      setRow(decided)
      link.reportSignOff(signOffId, 'approved')
    } catch (e: unknown) {
      setActionError(refusal(e, 'approve'))
    } finally {
      setBusy(null)
    }
  }

  async function decline(): Promise<void> {
    if (!client) return
    setBusy('decline')
    setActionError(null)
    try {
      const decided = await client.rest.json<SignOffOut>(`${path}/decline`, { method: 'POST' })
      setRow(decided)
      link.reportSignOff(signOffId, 'declined')
    } catch (e: unknown) {
      setActionError(refusal(e, 'decline'))
    } finally {
      setBusy(null)
    }
  }

  const open = row?.state === 'open'

  return (
    <div ref={box} className="bg-card p-5 text-foreground">
      <h1 className="text-sm font-semibold">
        {row ? `${row.site} asks for your approval` : 'Approve with your passkey'}
      </h1>
      {loadError && <p className="mt-2 text-[13px] text-destructive" role="alert">{loadError}</p>}
      {!row && !loadError && <div className="mt-3 h-16 animate-pulse rounded-md bg-muted" />}
      {row && (
        <>
          <p className="mt-2 text-[14px]" data-testid="sign-off-title">{row.action_title}</p>
          {row.action_detail && (
            <p className="mt-1 whitespace-pre-wrap text-[13px] text-foreground-secondary">{row.action_detail}</p>
          )}
          <p className="mt-2 text-[13px] text-foreground-secondary" data-testid="sign-off-for">
            For {row.approvers === 'agent_admins' && row.agent_name ? `the agent ${row.agent_name} (${row.subject})` : row.subject}
          </p>
          {actionError && <p className="mt-2 text-[13px] text-destructive" role="alert">{actionError}</p>}
          {open ? (
            <div className="mt-4 flex flex-wrap gap-2">
              <Button type="button" size="sm" disabled={busy !== null} onClick={() => void approve()}>
                {busy === 'approve' ? 'Waiting for your passkey…' : 'Approve with passkey'}
              </Button>
              <Button type="button" size="sm" variant="outline" disabled={busy !== null} onClick={() => void decline()}>
                {busy === 'decline' ? 'Declining…' : 'Decline'}
              </Button>
            </div>
          ) : (
            <p className="mt-3 text-[13px] text-foreground-secondary" data-testid="sign-off-state">
              {STATE_TEXT[row.state] ?? row.state}
            </p>
          )}
        </>
      )}
      {(loadError || (row && !open)) && (
        <div className="mt-4">
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => (row && isOver(row.state) ? link.reportSignOff(signOffId, row.state) : link.requestClose())}
          >
            Close
          </Button>
        </div>
      )}
    </div>
  )
}

function isOver(state: string): state is 'approved' | 'declined' | 'expired' {
  return state === 'approved' || state === 'declined' || state === 'expired'
}

function refusal(e: unknown, what: 'approve' | 'decline'): string {
  if (e instanceof CanopyRestError) {
    if (e.status === 409) {
      return /no_passkey|No passkey/i.test(e.message)
        ? 'You have no passkey in canopy yet. Add one in canopy Settings → Passkeys, then try again.'
        : 'This request was already decided, or has expired.'
    }
    if (e.status === 400) return 'Your passkey could not be checked. Try again.'
  }
  if (e instanceof DOMException && e.name === 'NotAllowedError') return 'The passkey prompt was cancelled.'
  return `Could not ${what}. Try again.`
}
