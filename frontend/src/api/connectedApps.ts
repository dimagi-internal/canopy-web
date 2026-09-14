// Connecting a site to canopy — the surface that replaced the Django admin.
//
// Owner-only, tenant-scoped, and shaped like `api/workspaces.ts` beside it:
// same `WorkspaceApiError` so a caller can branch on 404 (not a member) versus
// 403 (a member who is not an owner), and the same `res.response.ok` check
// rather than `res.error`, whose static type openapi-fetch infers as
// always-undefined for routes that declare no error model.
import { apiV2 } from './client.v2'
import { problemMessage } from './problem'
import { WorkspaceApiError } from './workspaces'
import type { components } from './generated'

export type ConnectedApp = components['schemas']['ConnectedAppOut']
export type ConnectedAppCreated = components['schemas']['ConnectedAppCreatedOut']

async function unwrap<T>(res: { response: Response; data?: unknown; error?: unknown }, what: string): Promise<T> {
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, what))
  }
  return res.data as T
}

export async function listConnectedApps(slug: string): Promise<ConnectedApp[]> {
  const res = await apiV2.GET('/api/workspaces/{slug}/connected-apps', {
    params: { path: { slug } },
  })
  return (await unwrap<ConnectedApp[]>(res, 'Could not load connected sites')) ?? []
}

export async function connectApp(
  slug: string,
  body: {
    name: string
    origins: string[]
    delegation_domains: string[]
    agents: string[]
    public_keys: string[]
  },
): Promise<ConnectedAppCreated> {
  const res = await apiV2.POST('/api/workspaces/{slug}/connected-apps', {
    params: { path: { slug } },
    body,
  })
  return unwrap<ConnectedAppCreated>(res, 'Could not connect the site')
}

export async function updateConnectedApp(
  slug: string,
  appId: number,
  body: {
    origins?: string[]
    delegation_domains?: string[]
    agents?: string[]
    public_keys?: string[]
  },
): Promise<ConnectedApp> {
  const res = await apiV2.PATCH('/api/workspaces/{slug}/connected-apps/{app_id}', {
    params: { path: { slug, app_id: appId } },
    body,
  })
  return unwrap<ConnectedApp>(res, 'Could not save the change')
}

export async function rotateSecret(slug: string, appId: number): Promise<string> {
  const res = await apiV2.POST('/api/workspaces/{slug}/connected-apps/{app_id}/rotate', {
    params: { path: { slug, app_id: appId } },
  })
  const body = await unwrap<{ secret: string }>(res, 'Could not issue a new secret')
  return body.secret
}

export async function disconnectApp(slug: string, appId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/workspaces/{slug}/connected-apps/{app_id}', {
    params: { path: { slug, app_id: appId } },
  })
  await unwrap<void>(res, 'Could not disconnect the site')
}

/** Turn canopy's own widget on here. The origin is taken from the request
 *  server-side — it is the address you are looking at, and the one value that
 *  cannot be typed wrong. */
export async function enableSelfWidget(slug: string, agents: string[]): Promise<ConnectedApp> {
  const res = await apiV2.POST('/api/workspaces/{slug}/connected-apps/enable-self', {
    params: { path: { slug } },
    body: { agents },
  })
  return unwrap<ConnectedApp>(res, 'Could not turn on the widget')
}
