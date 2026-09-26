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
    agents: string[]
    public_keys: string[]
    show_on_canopy_pages: boolean
    /** Where the site publishes its keys. Preferred over `public_keys`: canopy
     *  follows a rotation instead of needing a new paste. */
    jwks_url: string
    /** The site's OAuth issuer + MCP resource, when it grants canopy access to
     *  its tools AS the visitor (host grant contract v1). "" when it does not. */
    host_issuer: string
    host_mcp_resource: string
  },
): Promise<ConnectedApp> {
  const res = await apiV2.POST('/api/workspaces/{slug}/connected-apps', {
    params: { path: { slug } },
    body,
  })
  return unwrap<ConnectedApp>(res, 'Could not connect the site')
}

export async function updateConnectedApp(
  slug: string,
  appId: number,
  body: {
    origins?: string[]
    agents?: string[]
    public_keys?: string[]
    jwks_url?: string
    host_issuer?: string
    host_mcp_resource?: string
    show_on_canopy_pages?: boolean
  },
): Promise<ConnectedApp> {
  const res = await apiV2.PATCH('/api/workspaces/{slug}/connected-apps/{app_id}', {
    params: { path: { slug, app_id: appId } },
    body,
  })
  return unwrap<ConnectedApp>(res, 'Could not save the change')
}

export async function disconnectApp(slug: string, appId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/workspaces/{slug}/connected-apps/{app_id}', {
    params: { path: { slug, app_id: appId } },
  })
  await unwrap<void>(res, 'Could not disconnect the site')
}

