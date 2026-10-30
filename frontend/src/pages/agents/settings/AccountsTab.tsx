import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { AgentConnectionsPanel } from '@/pages/agents/AgentConnectionsPanel'
import { AgentAccountsPanel } from '@/pages/agents/AgentAccountsPanel'
import { Section } from '@/components/objectPage/settingsLayout'

// External accounts: GitHub and its Google mailbox.

export function AccountsTab({ agent, isAdmin }: { agent: AgentOutletContext['agent']; isAdmin: boolean }) {
  return (
    <>
    <Section id="accounts" title="External accounts">
      <AgentAccountsPanel agent={agent} />
      {isAdmin && <AgentConnectionsPanel agent={agent} />}
    </Section>
    </>
  )
}
