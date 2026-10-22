// An agent's declared cloud capabilities and the workspace admin's grants
// (docs/architecture/capabilities.md). The server decides who may grant
// (`integrations`, a workspace admin) and that a grant may only LOWER the
// manifest's limits; these wrappers surface its refusal text verbatim.
import { apiV2 } from './client.v2'
import { ensureOk } from './problem'
import type { components } from './generated'

export type AgentCapabilitiesOut = components['schemas']['AgentCapabilitiesOut']
export type AgentCapabilityOut = components['schemas']['AgentCapabilityOut']
export type CapabilityGrantOut = components['schemas']['CapabilityGrantOut']
export type CapabilityLeaseOut = components['schemas']['CapabilityLeaseOut']

export async function getAgentCapabilities(slug: string): Promise<AgentCapabilitiesOut> {
  const res = await apiV2.GET('/api/agents/{slug}/capabilities', { params: { path: { slug } } })
  ensureOk(res, 'Failed to load capabilities')
  return res.data as AgentCapabilitiesOut
}

export interface GrantLimits {
  /** Lower the manifest's concurrency; null keeps the manifest's. */
  max_concurrent: number | null
  /** Lower the manifest's lease length (seconds); null keeps the manifest's. */
  max_lease_s: number | null
}

/** Grant (or change the limits of an existing grant — the route is an upsert). */
export async function setCapabilityGrant(
  agent: string,
  capability: string,
  limits: GrantLimits,
): Promise<CapabilityGrantOut> {
  const res = await apiV2.PUT('/api/capabilities/grants', {
    body: { agent, capability, max_concurrent: limits.max_concurrent, max_lease_s: limits.max_lease_s },
  })
  ensureOk(res, 'The grant was refused')
  return res.data as CapabilityGrantOut
}

/** Revoke a grant; the server also releases every live lease it allowed. */
export async function revokeCapabilityGrant(grantId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/capabilities/grants/{grant_id}', {
    params: { path: { grant_id: grantId } },
  })
  ensureOk(res, 'The revoke was refused')
}
