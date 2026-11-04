import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { AgentAccountsPanel } from '@/pages/agents/AgentAccountsPanel'
import { Section } from '@/components/objectPage/settingsLayout'

// External accounts: one list of the outside accounts it acts as.

export function AccountsTab({ agent, isAdmin }: { agent: AgentOutletContext['agent']; isAdmin: boolean }) {
  return (
    <>
    <Section id="accounts" title="External accounts">
      <AgentAccountsPanel agent={agent} isAdmin={isAdmin} />
    </Section>
    </>
  )
}
