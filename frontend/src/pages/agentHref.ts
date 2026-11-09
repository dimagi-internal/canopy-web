import type { AgentOut } from '@/api/agents'
import { scopedPath } from '../lib/scopedLinks'

// Link to the agent's HOME workspace, not the flat `/agents/<slug>`. The flat
// route goes through TenantRedirect, which resolves the user's ACTIVE workspace
// — so once an agent has moved workspaces the card would send you to
// /w/<active>/agents/<slug>, a tenant it does not live in.
export function agentHref(agent: Pick<AgentOut, 'slug' | 'workspace'>): string {
  const slug = encodeURIComponent(agent.slug)
  return agent.workspace
    ? scopedPath(agent.workspace, `/agents/${slug}`)
    : `/agents/${slug}`
}
