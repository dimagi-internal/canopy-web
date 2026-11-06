import { useCallback, useEffect, useState, type FormEvent, type JSX } from 'react'
import { useLocation, useParams } from 'react-router-dom'
import { Button, Input } from 'canopy-ui/ui'
import { WorkbenchSubHeader } from 'canopy-ui'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { listAgents, type AgentOut } from '@/api/agents'
import {
  registerHostSite,
  revokeHostSite,
  listHostSites,
  updateHostSite,
  type HostSite,
} from '@/api/hostSites'
import { roleAllows } from '@/lib/workspaceRoles'
import { ApiError } from '@/api/problem'
import { parseKeys, parseOrigins, currentOrigin } from '@/pages/hostSites/parse'
import {
  AgentPicker,
  DefaultAgentPicker,
  HostGrantEditor,
  HostGrantFields,
} from '@/pages/hostSites/HostGrantEditor'
import { ConnectionTester } from '@/pages/hostSites/ConnectionTester'
import { SiteHealthTable } from '@/pages/hostSites/SiteHealthTable'
import { SiteNeedsSection } from '@/pages/hostSites/SiteNeedsTable'
import { FillFromSite, SiteAddressFill } from '@/pages/hostSites/SiteFactsTable'

/**
 * Connect a site to canopy — the page that replaced the Django admin.
 *
 * Every one of these settings fails CLOSED and silently: a wrong URL, a
 * missing agent, a name that does not match, and the only symptom is a
 * launcher that never appears. So the page's job is less "expose fields" than
 * "say what each one does and what happens when it is absent", which the admin
 * could not do at all.
 */

export function HostSitesPage(): JSX.Element | null {
  const { workspace: slug } = useParams()
  const { hash } = useLocation()
  const { workspaces } = useWorkspace()
  // Two tiers (lib/workspaceRoles): an admin reads the sites and runs Test
  // connection; only an owner registers, changes or disconnects one — a
  // registered key vouches for visitors as members of this workspace.
  const myRole = workspaces.find((w) => w.slug === slug)?.role
  const isOwner = roleAllows(myRole, 'own')
  const canTest = roleAllows(myRole, 'integrations')

  const [apps, setApps] = useState<HostSite[] | null>(null)
  const [agents, setAgents] = useState<AgentOut[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The site the form just connected. Its card lands in the list above, out
  // of sight of the button, so the form says so and links to it.
  const [connected, setConnected] = useState<HostSite | null>(null)

  const [name, setName] = useState('')
  const [origins, setOrigins] = useState('')
  const [showHere, setShowHere] = useState(false)
  const [picked, setPicked] = useState<string[]>([])
  const [signingKey, setSigningKey] = useState('')
  const [jwksUrl, setJwksUrl] = useState('')
  const [hostIssuer, setHostIssuer] = useState('')
  const [hostResource, setHostResource] = useState('')

  const reload = useCallback(async () => {
    if (!slug) return
    try {
      setApps(await listHostSites(slug))
      setLoadError(null)
    } catch (e) {
      setLoadError(
        e instanceof ApiError && e.status === 403
          ? 'Only a workspace admin or owner can see host sites.'
          : e instanceof Error
            ? e.message
            : 'Could not load host sites.',
      )
    }
  }, [slug])

  useEffect(() => {
    void reload()
    void listAgents({ limit: 100 })
      .then((p) => setAgents(p.items))
      .catch(() => setAgents([]))
  }, [reload])

  // `#site-<id>` is a site's own address (the site agents API's `settings_url`):
  // its card renders after the list loads, so scroll to it then.
  useEffect(() => {
    if (!apps || !hash.startsWith('#site-')) return
    document.getElementById(hash.slice(1))?.scrollIntoView?.({ block: 'start' })
  }, [apps, hash])

  async function run(what: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await what()
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Something went wrong.')
    } finally {
      setBusy(false)
    }
  }

  async function onConnect(e: FormEvent) {
    e.preventDefault()
    setConnected(null)
    await run(async () => {
      const app = await registerHostSite(slug!, {
        name: name.trim(),
        origins: parseOrigins(origins),
        show_on_canopy_pages: showHere,
        agents: picked,
        public_keys: parseKeys(signingKey),
        jwks_url: jwksUrl.trim(),
        host_issuer: hostIssuer.trim(),
        host_mcp_resource: hostResource.trim(),
      })
      setConnected(app)
      setName('')
      setOrigins('')
      setPicked([])
      setSigningKey('')
      setShowHere(false)
      setHostIssuer('')
      setHostResource('')
    })
  }

  if (!slug) return null

  return (
    <div className="max-w-4xl space-y-6">
      <WorkbenchSubHeader title="Host sites" count={apps?.length} />
      <p className="-mt-4 text-sm text-muted-foreground">
        Websites allowed to host one of this workspace's agents.
      </p>

      {!isOwner && (
        <p className="text-sm text-muted-foreground">
          Only a workspace owner can change these{canTest ? '; you can test them.' : '.'}
        </p>
      )}
      {loadError && <p className="text-sm text-destructive">{loadError}</p>}
      {error && <p className="text-sm text-destructive">{error}</p>}

      {apps && <SiteHealthTable apps={apps} />}

      {/* Everything already connected. */}
      <section className="space-y-3">
        <h2 className="text-sm font-medium text-foreground">Sites</h2>
        {apps === null && !loadError && (
          <p className="text-sm text-muted-foreground">Loading…</p>
        )}
        {apps?.length === 0 && (
          <p className="text-sm text-muted-foreground">No sites connected yet.</p>
        )}
        {apps
          ?.map((app) => (
            <div
              key={app.id}
              id={`site-${app.id}`}
              className={`rounded-lg border bg-card p-4 space-y-2 ${
                hash === `#site-${app.id}` || connected?.id === app.id ? 'border-primary' : 'border-border'
              }`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm text-foreground">
                    {app.name}
                    {app.revoked && (
                      <span className="ml-2 text-xs text-destructive">disconnected</span>
                    )}
                  </p>
                  <p className="truncate text-xs text-muted-foreground">
                    {app.origins.join(', ') || 'no URLs — the widget will not load'}
                  </p>
                  {app.shows_on_canopy_pages && (
                    <p className="text-xs text-primary">Shown on canopy's own pages</p>
                  )}
                  {app.asserts_verified_email && (
                    // Set by a canopy superuser, never here: the person a trusted
                    // address keys is canopy-wide. Changing the site's keys clears it.
                    <p className="text-xs text-primary">
                      Trusted for visitors' email — they can choose to let agents remember them
                    </p>
                  )}
                </div>
                {isOwner && !app.revoked && (
                  // This workspace's own registration. Another workspace using
                  // the same system registered it separately.
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy}
                    onClick={() => void run(() => revokeHostSite(slug, app.id))}
                  >
                    Disconnect
                  </Button>
                )}
              </div>
              {!app.revoked && (
                <AgentPicker
                  agents={agents}
                  selected={app.agents.map((a) => a.slug)}
                  onChange={(slugs) =>
                    void run(() => updateHostSite(slug, app.id, { agents: slugs }))
                  }
                />
              )}
              {isOwner && !app.revoked && app.agents.length > 1 && (
                <DefaultAgentPicker
                  app={app}
                  busy={busy}
                  onChange={(agent) =>
                    void run(() => updateHostSite(slug, app.id, { default_agent: agent }))
                  }
                />
              )}
              {isOwner && !app.revoked && (
                <HostGrantEditor
                  app={app}
                  busy={busy}
                  onSave={(issuer, resource) =>
                    void run(() =>
                      updateHostSite(slug, app.id, {
                        host_issuer: issuer,
                        host_mcp_resource: resource,
                      }),
                    )
                  }
                />
              )}
              {isOwner && !app.revoked && (
                <FillFromSite
                  slug={slug}
                  app={app}
                  busy={busy}
                  onSave={(facts) =>
                    void run(() =>
                      updateHostSite(slug, app.id, {
                        ...facts,
                        // Re-adds canopy's own origin, which the site does not list.
                        ...(app.shows_on_canopy_pages ? { show_on_canopy_pages: true } : {}),
                      }),
                    )
                  }
                />
              )}
              {canTest && !app.revoked && <SiteNeedsSection slug={slug} app={app} />}
              {canTest && !app.revoked && <ConnectionTester slug={slug} app={app} />}
            </div>
          ))}
      </section>

      {isOwner && (
        <form onSubmit={onConnect} className="rounded-lg border border-border bg-card p-4 space-y-3">
          <h2 className="text-sm font-medium text-foreground">Connect another site</h2>

          <SiteAddressFill
            slug={slug}
            onFill={(found) => {
              if (!name.trim()) setName(found.name)
              setOrigins(found.site.origins.join('\n'))
              setJwksUrl(found.site.jwks_url)
              setHostIssuer(found.site.issuer)
              setHostResource(found.site.mcp_resource)
            }}
          />

          <label className="block space-y-1">
            <span className="text-xs text-foreground-secondary">Name</span>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="connect-labs"
              required
            />
            <span className="block text-xs text-muted-foreground">
              Letters, digits and hyphens. Your site passes it to <code>canopy.init</code>.
            </span>
          </label>

          <label className="block space-y-1">
            <span className="text-xs text-foreground-secondary">Site URLs</span>
            <textarea
              value={origins}
              onChange={(e) => setOrigins(e.target.value)}
              rows={3}
              placeholder={`https://labs.connect.dimagi.com\nhttp://localhost:8000`}
              className="w-full rounded-md border border-input bg-input px-3 py-2 font-mono text-xs text-foreground"
            />
            <span className="block text-xs text-muted-foreground">
              One per line, including staging and local. No paths.
            </span>
          </label>

          <div className="space-y-1">
            <span className="text-xs text-foreground-secondary">Agents it may offer</span>
            <AgentPicker agents={agents} selected={picked} onChange={setPicked} />
          </div>

          <label className="block space-y-1">
            <span className="text-xs text-foreground-secondary">Signing keys URL (JWKS)</span>
            <input
              value={jwksUrl}
              onChange={(e) => setJwksUrl(e.target.value)}
              placeholder="https://your-site.example.com/.well-known/jwks.json"
              className="w-full rounded-md border border-input bg-input px-3 py-2 font-mono text-[11px] text-foreground"
            />
            <span className="block text-xs text-muted-foreground">
              Public https. canopy picks up key changes on its own.
            </span>
          </label>

          <label className="block space-y-1">
            <span className="text-xs text-foreground-secondary">…or paste a public key</span>
            <textarea
              value={signingKey}
              onChange={(e) => setSigningKey(e.target.value)}
              rows={4}
              placeholder={'-----BEGIN PUBLIC KEY-----\n…\n-----END PUBLIC KEY-----'}
              className="w-full rounded-md border border-input bg-input px-3 py-2 font-mono text-[11px] text-foreground"
            />
            <span className="block text-xs text-muted-foreground">
              Only if you have no JWKS URL. Never the private key.
            </span>
          </label>

          <div className="space-y-1">
            <span className="text-xs text-foreground-secondary">
              Let the agent use this site&apos;s tools as the visitor
            </span>
            <HostGrantFields
              issuer={hostIssuer}
              resource={hostResource}
              onIssuer={setHostIssuer}
              onResource={setHostResource}
            />
            <span className="block text-xs text-muted-foreground">
              Only if the site issues grants to canopy. Leave blank otherwise.
            </span>
          </div>

          {/* An ordinary option on an ordinary form. canopy used to have a
              section of its own here, driven by a reserved name that had to
              match a deployment setting — which made it a special case AND
              failed silently when the two drifted. */}
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={showHere}
              onChange={(e) => setShowHere(e.target.checked)}
              className="mt-1"
            />
            <span className="text-xs text-foreground-secondary">
              Also show the agent panel on canopy's own pages
              <span className="block text-muted-foreground">
                Adds {currentOrigin()} to the URLs. One site at a time.
              </span>
            </span>
          </label>

          <Button type="submit" disabled={busy || !name.trim()}>
            {busy ? 'Connecting…' : 'Connect'}
          </Button>
          {connected && !busy && (
            <p role="status" className="text-sm text-primary">
              Connected {connected.name}.{' '}
              <a href={`#site-${connected.id}`} className="underline">
                See its card
              </a>{' '}
              to choose which agent it opens on and test the connection.
            </p>
          )}
        </form>
      )}
    </div>
  )
}
