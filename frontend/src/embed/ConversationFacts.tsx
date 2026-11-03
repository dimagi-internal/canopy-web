import { ChevronDown, Lock } from 'lucide-react'
import { useState } from 'react'
import { PRIVATE_DETAILS, PRIVATE_LINE } from '@/components/chat/PrivateConversationNote'
import { SessionAudienceLine, type SessionAudience } from '@/components/SessionAudienceLine'
import { untilLabel, type PageGrant } from './pageGrant'
import { useConfirmedGrant } from './PageGrantConfirm'

/** The conversation's facts in one line under the header — private or not, the
 *  page, who can read it, the act-as-you permission — with the full sentences a
 *  click away. Stop stays on the line: ending a permission is never behind a click. */
export function ConversationFacts({ pageName, audience, sensitive, pageGrant, agentName }: {
  pageName: string
  audience: SessionAudience | null
  sensitive: boolean
  pageGrant?: PageGrant
  agentName: string
}) {
  const [open, setOpen] = useState(false)
  const [stopping, setStopping] = useState(false)
  const grant = useConfirmedGrant(pageGrant)
  const summary = [sensitive ? 'Private' : '', pageName, audience?.label ?? ''].filter(Boolean)
  if (!summary.length && !grant) return null
  const who = grant ? grant.agent_name || agentName : ''
  const until = grant ? untilLabel(grant.expires_at) : ''
  return (
    <div className="border-b border-border px-3 py-1 text-[11px] text-muted-foreground" data-testid="conversation-facts">
      <div className="flex items-center gap-2">
        <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}
                title={open ? 'Hide details' : 'Show details'}
                className="flex min-w-0 flex-1 items-center gap-1 text-left hover:text-foreground">
          {sensitive && <Lock className="h-3 w-3 shrink-0" aria-hidden />}
          <span className="min-w-0 truncate">{summary.join(' · ')}</span>
          <ChevronDown className={`h-3 w-3 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} aria-hidden />
        </button>
        {grant && pageGrant && (
          <span className="flex shrink-0 items-center gap-1" data-testid="page-grant-line"
                title={`What ${grant.site} lets ${who} read as you: ${grant.scope || 'this page'}`}>
            <span>{who} can act as you{until ? ` until ${until}` : ''}</span>
            <span aria-hidden>·</span>
            <button type="button" disabled={stopping}
                    onClick={() => { setStopping(true); void pageGrant.stop().finally(() => setStopping(false)) }}
                    className="text-destructive hover:underline disabled:opacity-50">
              Stop
            </button>
          </span>
        )}
      </div>
      {open && (
        <div className="mt-1 space-y-0.5" data-testid="conversation-facts-details">
          {pageName && (
            <p className="m-0" data-testid="asking-about">
              Asking about <span className="text-foreground">{pageName}</span>
            </p>
          )}
          <SessionAudienceLine audience={audience} />
          {sensitive && <p className="m-0" data-testid="sensitive-details">{PRIVATE_LINE}. {PRIVATE_DETAILS}</p>}
          {grant && (
            <p className="m-0">
              {who} can act as you on this page ({grant.scope || 'this page'} on {grant.site})
              {until ? ` until ${until}` : ''}.
            </p>
          )}
        </div>
      )}
    </div>
  )
}
