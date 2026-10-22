/**
 * API client for /api/system — the canopy capability catalog (skills / agents /
 * commands) read from the bundled plugin.
 */

import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

type Schemas = components['schemas']

export type CapabilityKind = 'skill' | 'agent' | 'command'

// The schema types `kind` as a plain string; the server only ever sends these three.
export type CapabilityItem = Omit<Schemas['CapabilityItemOut'], 'kind'> & { kind: CapabilityKind }
export type CapabilityDetail = Omit<Schemas['CapabilityDetailOut'], 'kind'> & { kind: CapabilityKind }
export type CapabilityCatalog = Omit<Schemas['CapabilityCatalogOut'], 'items'> & {
  items: readonly CapabilityItem[]
}

export async function getCatalog(): Promise<CapabilityCatalog> {
  const res = await apiV2.GET('/api/system/overview')
  return unwrap(res, 'getCatalog') as unknown as CapabilityCatalog
}

export async function getCapability(kind: CapabilityKind, name: string): Promise<CapabilityDetail> {
  const res = await apiV2.GET('/api/system/{kind}/{name}', { params: { path: { kind, name } } })
  return unwrap(res, 'getCapability') as unknown as CapabilityDetail
}
