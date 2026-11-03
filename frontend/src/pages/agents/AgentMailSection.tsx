import { useEffect, useMemo, useState } from 'react'
import { Link, useOutletContext, useParams } from 'react-router-dom'
import { getAgentMailbox, type AgentMailboxOut } from '@/api/mail'
import { AgentAddressList } from '@/components/agents/AgentAddressList'
import { MailClient } from '@/components/mail/MailClient'
import { scopedPath } from '@/lib/scopedLinks'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'

// An agent's mailbox: the same client as a person's own mail (components/mail/MailClient), reading
// the agent's threads and — for someone who may act as it — replying and composing as the agent.
export function AgentMailSection() {
  const { agent, canEdit } = useOutletContext<AgentOutletContext>()
  const { workspace = '' } = useParams()
  const [addresses, setAddresses] = useState<AgentMailboxOut['addresses']>([])
  const source = useMemo(() => ({ agent: agent.slug }), [agent.slug])

  useEffect(() => {
    let alive = true
    getAgentMailbox(agent.slug)
      .then((b) => alive && setAddresses(b.addresses ?? []))
      .catch(() => undefined) // the list is a nicety; the mail below still loads
    return () => {
      alive = false
    }
  }, [agent.slug])

  return (
    <div className="px-6 py-8">
      <div className="mb-4">
        <AgentAddressList addresses={addresses} />
      </div>
      <MailClient
        workspace={workspace}
        source={source}
        canSend={canEdit}
        sendNote={`Plain text, sent as ${agent.name}.`}
        empty={({ agentAddress }) => agentAddress ? (
          <p className="text-[13px] text-muted-foreground">No mail yet for {agentAddress}.</p>
        ) : (
          <p className="text-[13px] text-muted-foreground">
            This agent has no email address — an admin can add one in{' '}
            <Link to={scopedPath(workspace, '/settings/email')} className="text-primary underline">
              Settings → Email
            </Link>
            .
          </p>
        )}
      />
    </div>
  )
}
