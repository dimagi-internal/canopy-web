import { useEffect, useState, type JSX } from 'react'
import { Button, Input } from 'canopy-ui/ui'
import { getAgentMailbox, setAgentMailbox, type AgentMailboxOut } from '@/api/mail'
import { AgentAddressList } from './AgentAddressList'
import { scopedPath } from '../../lib/scopedLinks'

/**
 * The agent's email address, and where it lives. On one of the workspace's
 * canopy-hosted domains there is nothing to sign in: saving the address routes
 * it to the agent (apps/mail/domains.ensure_agent_route). Anywhere else it is a
 * Google mailbox, connected under Credentials. Any other addresses the agent holds
 * (more canopy-hosted routes, Gmail mailboxes) are listed under it.
 */
export function AgentMailboxControl({ slug, workspace, canEdit }: {
  slug: string
  workspace: string
  canEdit: boolean
}): JSX.Element {
  const [box, setBox] = useState<AgentMailboxOut | null>(null)
  const [draft, setDraft] = useState('')
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    getAgentMailbox(slug).then(setBox).catch(() => setBox({ kind: 'none' } as AgentMailboxOut))
  }, [slug])

  if (box === null) return <p className="text-[12px] text-muted-foreground">Loading…</p>

  const save = async () => {
    setBusy(true)
    setError('')
    try {
      const next = await setAgentMailbox(slug, draft.trim())
      setBox(next)
      setEditing(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save the address')
    } finally {
      setBusy(false)
    }
  }

  const emailPage = scopedPath(workspace, '/settings/email')
  const status = (() => {
    if (box.kind === 'none') return <span className="text-muted-foreground">No address.</span>
    if (box.kind === 'google') {
      return <span className="text-muted-foreground">Google mailbox — connect it under <a className="text-primary hover:underline" href="#credentials">Credentials</a>.</span>
    }
    if (box.live) return <span className="text-success" data-testid="mailbox-hosted-live">Canopy-hosted ✓ — nothing to sign in; mail to it is this agent&rsquo;s turn.</span>
    const why = box.route === 'taken' ? 'that address already goes somewhere else'
      : !box.verified ? `${box.domain} is waiting for its DNS records`
        : box.can_route ? 'not routed yet — save it again to route it'
          : 'a workspace admin routes it'
    return <span className="text-warning">Canopy-hosted, {why} (<a className="text-primary hover:underline" href={emailPage}>email settings</a>).</span>
  })()

  return (
    <div className="space-y-2 text-[13px]" data-testid="agent-mailbox">
      {!editing ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-foreground">{box.address || '—'}</span>
          {canEdit && (
            <Button size="sm" variant="outline" onClick={() => { setDraft(box.address ?? ''); setEditing(true) }}
                    data-testid="agent-mailbox-edit">
              {box.address ? 'Change' : 'Add an address'}
            </Button>
          )}
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Input value={draft} onChange={(e) => setDraft(e.target.value)} className="w-full sm:w-72"
                 placeholder={box.hosted_domains?.length ? `${slug}@${box.hosted_domains[0]}` : `${slug}@example.org`}
                 aria-label="Agent email address (Google or canopy-hosted)" data-testid="agent-mailbox-input" />
          <Button size="sm" disabled={busy} onClick={() => void save()} data-testid="agent-mailbox-save">
            {busy ? 'Saving…' : 'Save'}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(false)}>Cancel</Button>
        </div>
      )}
      <p className="text-[12px]">{status}</p>
      {(box.addresses?.some((a) => !a.primary) ?? false) && (
        <div className="space-y-1">
          <p className="text-[12px] text-muted-foreground">Also receives and sends as:</p>
          <AgentAddressList addresses={box.addresses} skipPrimary />
        </div>
      )}
      {editing && (box.hosted_domains?.length ?? 0) > 0 && (
        <p className="text-[12px] text-muted-foreground">
          Canopy-hosted here: {box.hosted_domains?.map((d) => `@${d}`).join(', ')} — no sign-in needed.
        </p>
      )}
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </div>
  )
}
