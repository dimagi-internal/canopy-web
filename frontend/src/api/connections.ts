// OAuth connections, whose tokens canopy-web can read only with the owner's switch on: `apps/connections/api.py`. Which providers exist
// (and what to call them) comes from the API's `providers`, never from here.
//
// CONNECT is not here. Like GitHub it is a full-page navigation to
// `/auth/connections/<provider>/<workspace>/start/` — the workspace whose app (OAuth
// client) it connects with, from the provider row — (a link for a person, a form
// POST for an agent) — the provider's consent screen has to be shown by the provider — and
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
export type ShareStateOut = components['schemas']['ShareStateOut']
export type ShareIn = components['schemas']['ShareIn']

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

export type DisconnectOut = components['schemas']['DisconnectOut']

export async function disconnectMine(id: string): Promise<DisconnectOut> {
  const res = await apiV2.DELETE('/api/connections/{connection_id}', {
    params: { path: { connection_id: id } },
  })
  return unwrap(res, 'Could not disconnect.')
}

export type TemporaryEndedOut = components['schemas']['TemporaryEndedOut']

export async function endTemporaryAccess(id: string): Promise<TemporaryEndedOut> {
  const res = await apiV2.DELETE('/api/connections/temporary/{access_id}', {
    params: { path: { access_id: id } },
  })
  return unwrap(res, 'Could not end it.')
}

// ---- Sharing: what the owner's browser reads and writes (`vault/connectionShare.ts`) ----
// canopy-web opens these envelopes only if the owner's statement names it. Sharing needs a fresh passkey; the step-up
// middleware answers that, and refusals carry their `code` on the `ApiError`.

export async function getShareState(id: string): Promise<ShareStateOut> {
  const res = await apiV2.GET('/api/connections/{connection_id}/share', {
    params: { path: { connection_id: id } },
  })
  return unwrap(res, 'Could not read this connection.') as unknown as ShareStateOut
}

export async function putShare(id: string, body: ShareIn): Promise<ConnectionOut> {
  const res = await apiV2.PUT('/api/connections/{connection_id}/share', {
    params: { path: { connection_id: id } },
    body,
  })
  return unwrap(res, 'Could not share this connection.') as unknown as ConnectionOut
}

/** Stop canopy-web using a connection (no passkey: giving up access is always easy). Turning it on is
 *  part of the signed share, so only `false` is sent. A shared connection answers 409 `needs_share`. */
export async function turnOffServerUse(id: string): Promise<ConnectionOut> {
  const res = await apiV2.POST('/api/connections/{connection_id}/server-use', {
    params: { path: { connection_id: id } },
    body: { on: false },
  })
  return unwrap(res, 'Could not turn this off.') as unknown as ConnectionOut
}

export async function listAgentConnections(slug: string): Promise<ConnectionsView> {
  const res = await apiV2.GET('/api/agents/{slug}/connections', { params: { path: { slug } } })
  return view(unwrap(res, 'Could not read the agent’s connections.'))
}

export async function disconnectAgentConnection(slug: string, id: string): Promise<DisconnectOut> {
  const res = await apiV2.DELETE('/api/agents/{slug}/connections/{connection_id}', {
    params: { path: { slug, connection_id: id } },
  })
  return unwrap(res, 'Could not disconnect.')
}

/** Where a connect starts. A person's is a GET link (`connectionStartPath`); an
 *  AGENT's is a form POST to this, CSRF-checked — the server refuses a GET that
 *  names an agent, so a link cannot attach an admin's own provider login to it. */
export function connectionStartAction(provider: string, workspace: string): string {
  const base = import.meta.env.BASE_URL.replace(/\/$/, '')
  return `${base}/auth/connections/${encodeURIComponent(provider)}/${encodeURIComponent(workspace)}/start/`
}

/** The full-page navigation that starts a PERSON's connect. `returnTo` is a router
 *  path (the server adds the deployment prefix when it sends the browser back). */
export function connectionStartPath(provider: string, workspace: string, returnTo: string): string {
  const q = new URLSearchParams({ return_to: returnTo })
  return `${connectionStartAction(provider, workspace)}?${q.toString()}`
}

// ---- A workspace's connection apps (its OAuth clients), admin-only ----------
// Saving or removing one needs a fresh passkey; the step-up middleware answers
// that, as for disconnecting an agent's connection.

export type ConnectionAppOut = components['schemas']['ConnectionAppOut']
export type ConnectionAppIn = components['schemas']['ConnectionAppIn']
export type OptionFieldOut = components['schemas']['OptionFieldOut']
export type ScopeChoiceOut = components['schemas']['ScopeChoiceOut']

export async function listConnectionApps(workspace: string): Promise<ConnectionAppOut[]> {
  const res = await apiV2.GET('/api/workspaces/{workspace}/connection-apps', { params: { path: { workspace } } })
  const d = unwrap(res, 'Could not read the workspace’s connection apps.') as unknown as { apps: ConnectionAppOut[] }
  return [...d.apps]
}

export async function configureConnectionApp(
  workspace: string, provider: string, body: ConnectionAppIn,
): Promise<ConnectionAppOut> {
  const res = await apiV2.PUT('/api/workspaces/{workspace}/connection-apps/{provider}', {
    params: { path: { workspace, provider } },
    body,
  })
  return unwrap(res, 'Could not save the app.') as unknown as ConnectionAppOut
}

export async function removeConnectionApp(workspace: string, provider: string): Promise<void> {
  const res = await apiV2.DELETE('/api/workspaces/{workspace}/connection-apps/{provider}', {
    params: { path: { workspace, provider } },
  })
  ensureOk(res, 'Could not remove the app.')
}

// ---- Screens that may use an agent's connection (project and app shares) ----
export type SurfaceShare = components['schemas']['SurfaceShareOut']
export type SurfaceSharePreview = components['schemas']['SurfaceSharePreviewOut']
export type SurfaceKind = 'project' | 'app'
export type SurfaceLine = { method: string; path: string }

export async function listSurfaceShares(id: string): Promise<SurfaceShare[]> {
  const res = await apiV2.GET('/api/connections/{connection_id}/surface-shares', {
    params: { path: { connection_id: id } },
  })
  return Array.from(unwrap(res, 'Could not read which screens use this.').shares) as SurfaceShare[]
}

export async function previewSurfaceShare(id: string, kind: SurfaceKind, ref: string): Promise<SurfaceSharePreview> {
  const res = await apiV2.GET('/api/connections/{connection_id}/surface-shares/preview', {
    params: { path: { connection_id: id }, query: { kind, ref } },
  })
  return unwrap(res, 'Could not read what that screen asks for.') as unknown as SurfaceSharePreview
}

/** Needs a fresh passkey; the step-up middleware asks for it. 409 `declaration_changed` if the screen changed. */
export async function approveSurfaceShare(
  id: string, kind: SurfaceKind, ref: string, expected: { reads: readonly SurfaceLine[]; writes: readonly SurfaceLine[] },
): Promise<SurfaceShare> {
  const res = await apiV2.POST('/api/connections/{connection_id}/surface-shares', {
    params: { path: { connection_id: id } },
    body: { kind, ref, expected_reads: [...expected.reads], expected_writes: [...expected.writes] },
  })
  return unwrap(res, 'Could not approve that.') as unknown as SurfaceShare
}

export async function stopSurfaceShare(id: string, shareId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/connections/{connection_id}/surface-shares/{share_id}', {
    params: { path: { connection_id: id, share_id: shareId } },
  })
  ensureOk(res, 'Could not stop that.')
}
