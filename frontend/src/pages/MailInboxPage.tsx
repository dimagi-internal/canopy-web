import { type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'

import { MailClient } from '@/components/mail/MailClient'
import { scopedPath } from '@/lib/scopedLinks'

// Your own mail: copies of what forwards to you (Settings → Email → Keep a copy) and what you send
// from those addresses. Private to you: not admins, not other members. The client itself is shared
// with an agent's Mail tab.
const SOURCE = { forwarded: true } as const

export function MailInboxPage(): JSX.Element {
  const { workspace = '' } = useParams()
  return (
    <MailClient
      workspace={workspace}
      source={SOURCE}
      canSend
      title="Mail"
      sendNote="Plain text, sent from your own address."
      empty={({ addresses }) => addresses.length > 0 ? null : (
        <p className="text-[13px] text-muted-foreground">
          Nothing yet. An admin turns this on per address with “Keep a copy in canopy” in{' '}
          <Link to={scopedPath(workspace, '/settings/email')} className="text-primary underline">Settings → Email</Link>.
          Only mail that arrives after that is kept. Only you can see it.
        </p>
      )}
    />
  )
}
