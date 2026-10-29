// Server-held OAuth connections (Salesforce first): `apps/connections/api.py`.
//
// CONNECT is not here. Like GitHub it is a full-page navigation to
// `/auth/connections/<provider>/start/` (a link for a person, a form POST for an
// agent) — the provider's consent screen has to be shown by the provider — and
// it returns to `return_to` with
// `?connection=<provider>&status=connected|error&reason=<code>`.
//
// Disconnecting an AGENT's connection needs a fresh passkey; that is answered by
// the API client's step-up middleware (`auth/stepUp.ts`), so nothing here knows.
import { apiV2 } from './client.v2'
import { ensureOk, unwrap } from './problem'
import type { components } from './generated'

export type ConnectionOut = components['schemas']['ConnectionOut']
export type ProviderOut = components['schemas']['ProviderOut']

export interface ConnectionsView {
  providers: ProviderOut[]
  connections: ConnectionOut[]
}

// The `--immutable` codegen types arrays as non-iterable objects; cast through
// `unknown` (the idiom in api/github.ts and api/workspaces.ts).
function view(d: unknown): ConnectionsView {
  const v = d as ConnectionsView
  return { providers: [...v.providers], connections: [...v.connections] }
}

export async function listMyConnections(): Promise<ConnectionsView> {
  const res = await apiV2.GET('/api/connections/', {})
  return view(unwrap(res, 'Could not read your connections.'))
}

export async function disconnectMine(id: number): Promise<void> {
  const res = await apiV2.DELETE('/api/connections/{connection_id}', {
    params: { path: { connection_id: id } },
  })
  ensureOk(res, 'Could not disconnect.')
}

export async function listAgentConnections(slug: string): Promise<ConnectionsView> {
  const res = await apiV2.GET('/api/agents/{slug}/connections', { params: { path: { slug } } })
  return view(unwrap(res, 'Could not read the agent’s connections.'))
}

export async function disconnectAgentConnection(slug: string, id: number): Promise<void> {
  const res = await apiV2.DELETE('/api/agents/{slug}/connections/{connection_id}', {
    params: { path: { slug, connection_id: id } },
  })
  ensureOk(res, 'Could not disconnect.')
}

export async function grantConnection(slug: string, id: number, other: string): Promise<void> {
  const res = await apiV2.PUT('/api/agents/{slug}/connections/{connection_id}/grants/{other_slug}', {
    params: { path: { slug, connection_id: id, other_slug: other } },
  })
  ensureOk(res, 'Could not share the connection.')
}

export async function revokeConnectionGrant(slug: string, id: number, other: string): Promise<void> {
  const res = await apiV2.DELETE('/api/agents/{slug}/connections/{connection_id}/grants/{other_slug}', {
    params: { path: { slug, connection_id: id, other_slug: other } },
  })
  ensureOk(res, 'Could not stop sharing the connection.')
}

/** Where a connect starts. A person's is a GET link (`connectionStartPath`); an
 *  AGENT's is a form POST to this, CSRF-checked — the server refuses a GET that
 *  names an agent, so a link cannot attach an admin's Salesforce login to it. */
export function connectionStartAction(provider: string): string {
  const base = import.meta.env.BASE_URL.replace(/\/$/, '')
  return `${base}/auth/connections/${encodeURIComponent(provider)}/start/`
}

/** The full-page navigation that starts a PERSON's connect. `returnTo` is a router
 *  path (the server adds the deployment prefix when it sends the browser back). */
export function connectionStartPath(provider: string, returnTo: string): string {
  const q = new URLSearchParams({ return_to: returnTo })
  return `${connectionStartAction(provider)}?${q.toString()}`
}
