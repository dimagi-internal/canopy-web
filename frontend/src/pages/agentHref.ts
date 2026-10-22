import type { AgentOut } from '@/api/agents'
import { scopedPath } from '../lib/scopedLinks'

// Link to the agent's HOME workspace, not the flat `/agents/<slug>`. The flat
// route goes through TenantRedirect, which resolves the user's ACTIVE workspace
// — so once an agent moved workspaces (Ada, connect → dimagi, 2026-10-03) the
// card sent you to /w/<active>/agents/<slug>, a tenant it no longer lives in.
export function agentHref(agent: Pick<AgentOut, 'slug' | 'workspace'>): string {
  const slug = encodeURIComponent(agent.slug)
  return agent.workspace
    ? scopedPath(agent.workspace, `/agents/${slug}`)
    : `/agents/${slug}`
}
