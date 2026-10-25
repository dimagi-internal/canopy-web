// Connecting a site to canopy — the surface that replaced the Django admin.
//
// Owner-only, tenant-scoped, and shaped like `api/workspaces.ts` beside it:
// same `WorkspaceApiError` so a caller can branch on 404 (not a member) versus
// 403 (a member who is not an owner), and the same `res.response.ok` check
// rather than `res.error`, whose static type openapi-fetch infers as
// always-undefined for routes that declare no error model.
import { apiV2 } from './client.v2'
import { ensureOk } from './problem'
import type { components } from './generated'

export type ConnectedApp = components['schemas']['ConnectedAppOut']

// A refusal is the shared `ApiError` (./problem): `.status` tells 404 (not a
// member) from 403 (a member who is not an owner).
async function unwrap<T>(res: { response: Response; data?: unknown; error?: unknown }, what: string): Promise<T> {
  ensureOk(res, what)
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
    /** One of `agents` for its panel to open on; "" (the default) for none. */
    default_agent?: string
  },
): Promise<ConnectedApp> {
  const res = await apiV2.POST('/api/workspaces/{slug}/connected-apps', {
    params: { path: { slug } },
    body: { ...body, default_agent: body.default_agent ?? '' },
  })
  return unwrap<ConnectedApp>(res, 'Could not connect the site')
}

export async function updateConnectedApp(
  slug: string,
  appId: number,
  body: {
    origins?: string[]
    agents?: string[]
    /** One of the site's agents, or '' to clear it. */
    default_agent?: string
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

export type ConnectionTest = components['schemas']['ConnectionTestOut']
export type ConnectionCheck = components['schemas']['ConnectionCheckOut']

/** Run the SDK's conformance checks against this site's settings, from
 *  canopy's server. Owner-only, like everything else here. */
export async function testConnectedApp(slug: string, appId: number): Promise<ConnectionTest> {
  const res = await apiV2.POST('/api/workspaces/{slug}/connected-apps/{app_id}/test', {
    params: { path: { slug, app_id: appId } },
  })
  return unwrap<ConnectionTest>(res, 'Could not test the connection')
}

export async function disconnectApp(slug: string, appId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/workspaces/{slug}/connected-apps/{app_id}', {
    params: { path: { slug, app_id: appId } },
  })
  await unwrap<void>(res, 'Could not disconnect the site')
}


export type SiteNeeds = components['schemas']['SiteNeedsOut']
export type AgentFit = components['schemas']['AgentFitOut']

/** What the site asks of agents (its signed `site.json`), and per agent canopy
 *  allows there whether it meets it. Fail-closed: `available: false` + reason. */
export async function getConnectedAppNeeds(slug: string, appId: number): Promise<SiteNeeds> {
  const res = await apiV2.GET('/api/workspaces/{slug}/connected-apps/{app_id}/needs', {
    params: { path: { slug, app_id: appId } },
  })
  return unwrap<SiteNeeds>(res, "Could not read what the site needs")
}

export type SiteDiscovery = components['schemas']['SiteDiscoveryOut']
export type SiteFacts = components['schemas']['SiteFactsOut']

/** The facts a site declares about itself in its signed `site.json` — origins,
 *  keys URL, sign-in issuer, MCP server — for an owner to adopt. Read-only:
 *  nothing changes until the owner saves them through `connectApp` /
 *  `updateConnectedApp`. `address` may be the site's address, its keys URL or the
 *  document's; with `appId` it is optional (canopy looks beside the site's keys). */
export async function discoverSite(
  slug: string,
  opts: { address?: string; appId?: number },
): Promise<SiteDiscovery> {
  const res = await apiV2.GET('/api/workspaces/{slug}/connected-apps/discover', {
    params: {
      path: { slug },
      query: { address: opts.address ?? '', ...(opts.appId !== undefined ? { app_id: opts.appId } : {}) },
    },
  })
  return unwrap<SiteDiscovery>(res, "Could not read the site's own documents")
}
