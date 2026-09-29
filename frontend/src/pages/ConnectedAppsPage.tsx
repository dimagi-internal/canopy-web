import { useCallback, useEffect, useState, type FormEvent, type JSX } from 'react'
import { useParams } from 'react-router-dom'
import { Button, Input } from 'canopy-ui/ui'
import { WorkbenchSubHeader } from 'canopy-ui'

import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { listAgents, type AgentOut } from '@/api/agents'
import { relativeTime } from '@/components/activity/turnLog'
import {
  connectApp,
  disconnectApp,
  listConnectedApps,
  testConnectedApp,
  updateConnectedApp,
  type ConnectedApp,
  type ConnectionCheck,
  type ConnectionTest,
} from '@/api/connectedApps'
import { WorkspaceApiError } from '@/api/workspaces'

/**
 * Connect a site to canopy — the page that replaced the Django admin.
 *
 * Every one of these settings fails CLOSED and silently: a wrong URL, a
 * missing agent, a name that does not match, and the only symptom is a
 * launcher that never appears. So the page's job is less "expose fields" than
 * "say what each one does and what happens when it is absent", which the admin
 * could not do at all.
 */

/** Split a textarea of URLs into a list. One per line, and commas too, because
 *  people paste both and neither is wrong. */
export function parseOrigins(raw: string): string[] {
  return raw
    .split(/[\n,]/)
    .map((s) => s.trim().replace(/\/$/, ''))
    .filter(Boolean)
}

/** Split a textarea of PEM blocks into a list.
 *
 *  Several keys is the normal state during a rotation, not an edge case: you
 *  publish the new one beside the old, switch the signer, then remove the old.
 *  Splitting on the END marker rather than on blank lines keeps a key whose
 *  base64 body happens to contain one. */
export function parseKeys(raw: string): string[] {
  const END = '-----END PUBLIC KEY-----'
  return raw
    .split(END)
    .map((chunk) => chunk.trim())
    .filter((chunk) => chunk.includes('BEGIN'))
    .map((chunk) => `${chunk}\n${END}`)
}

/** The origin of the page you are on, which is the value the self-widget needs
 *  and the likeliest thing someone wants for a first connection. */
export function currentOrigin(): string {
  return typeof window === 'undefined' ? '' : window.location.origin
}

function AgentPicker({
  agents,
  selected,
  onChange,
}: {
  agents: AgentOut[]
  selected: string[]
  onChange: (slugs: string[]) => void
}): JSX.Element {
  if (agents.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        This workspace has no agents yet, so there is nothing to offer.
      </p>
    )
  }
  return (
    <div className="flex flex-wrap gap-2">
      {agents.map((a) => {
        const on = selected.includes(a.slug)
        return (
          <button
            key={a.slug}
            type="button"
            aria-pressed={on}
            onClick={() =>
              onChange(on ? selected.filter((s) => s !== a.slug) : [...selected, a.slug])
            }
            className={`rounded-full border px-3 py-1 text-xs transition-colors ${
              on
                ? 'border-primary bg-primary/10 text-primary'
                : 'border-border text-foreground-secondary hover:bg-muted'
            }`}
          >
            {a.name}
          </button>
        )
      })}
    </div>
  )
}

/**
 * Where the site's own OAuth server and MCP server are, so the agent can call
 * the site's tools AS the visitor (host grant contract v1). Both blank is an
 * ordinary state: the site grants nothing, and the agent cannot act for a
 * visitor there. canopy never falls back to the agent's own login.
 */
function HostGrantFields({
  issuer,
  resource,
  onIssuer,
  onResource,
}: {
  issuer: string
  resource: string
  onIssuer: (v: string) => void
  onResource: (v: string) => void
}): JSX.Element {
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      <label className="block space-y-1">
        <span className="text-xs text-foreground-secondary">Sign-in issuer (optional)</span>
        <input
          value={issuer}
          onChange={(e) => onIssuer(e.target.value)}
          placeholder="https://labs.connect.dimagi.com"
          className="w-full rounded-md border border-input bg-input px-3 py-2 font-mono text-[11px] text-foreground"
        />
      </label>
      <label className="block space-y-1">
        <span className="text-xs text-foreground-secondary">MCP server (optional)</span>
        <input
          value={resource}
          onChange={(e) => onResource(e.target.value)}
          placeholder="https://labs.connect.dimagi.com/mcp/"
          className="w-full rounded-md border border-input bg-input px-3 py-2 font-mono text-[11px] text-foreground"
        />
      </label>
    </div>
  )
}

function HostGrantEditor({
  app,
  busy,
  onSave,
}: {
  app: ConnectedApp
  busy: boolean
  onSave: (issuer: string, resource: string) => void
}): JSX.Element {
  const [issuer, setIssuer] = useState(app.host_issuer)
  const [resource, setResource] = useState(app.host_mcp_resource)
  const dirty = issuer.trim() !== app.host_issuer || resource.trim() !== app.host_mcp_resource
  return (
    <div className="space-y-2 border-t border-border pt-2">
      <p className="text-xs text-muted-foreground">
        {app.issues_host_grants
          ? 'The agent can use this site\'s tools as the visitor, when the site grants it.'
          : 'The agent cannot act as a visitor on this site.'}
      </p>
      <HostGrantFields issuer={issuer} resource={resource} onIssuer={setIssuer} onResource={setResource} />
      {dirty && (
        <Button size="sm" disabled={busy} onClick={() => onSave(issuer.trim(), resource.trim())}>
          Save
        </Button>
      )}
    </div>
  )
}

const STATUS_STYLE: Record<ConnectionCheck['status'], string> = {
  pass: 'bg-success/10 text-success border-success/30',
  fail: 'bg-destructive/10 text-destructive border-destructive/30',
  skip: 'bg-muted text-muted-foreground border-border',
}

/** One row per check: status, what, why. Shared by the settings checks and the
 *  live probe, which report in the same shape. */
function CheckRows({ checks }: { checks: readonly ConnectionCheck[] }): JSX.Element {
  return (
    <table className="w-full border-collapse text-xs">
      <thead>
        <tr className="border-b border-border text-left text-muted-foreground">
          <th className="w-14 py-1 pr-2 font-normal">Result</th>
          <th className="py-1 pr-2 font-normal">Check</th>
          <th className="py-1 font-normal">Detail</th>
        </tr>
      </thead>
      <tbody>
        {checks.map((c, i) => (
          <tr key={`${c.name}-${i}`} className="border-b border-border align-top last:border-0">
            <td className="py-1 pr-2">
              <span className={`inline-block rounded border px-1.5 text-[10px] uppercase ${STATUS_STYLE[c.status]}`}>
                {c.status}
              </span>
            </td>
            <td className="py-1 pr-2 text-foreground" title={c.name}>
              {c.label}
            </td>
            <td className="break-all py-1 font-mono text-[11px] text-foreground-secondary">{c.detail}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** What the live probe concluded, in one sentence. */
export function liveProbeSummary(result: Pick<ConnectionTest, 'live_probe' | 'live_probe_ok'>): string {
  const steps = result.live_probe ?? []
  if (result.live_probe_ok === true) {
    return 'Live probe passed: a real grant was issued, redeemed and used.'
  }
  const failed = steps.find((s) => s.status === 'fail')
  if (result.live_probe_ok === false && failed) return `Live probe failed: ${failed.label}.`
  const skipped = steps.find((s) => s.status === 'skip')
  return `Live probe not run: ${skipped?.detail || 'no verdict'}.`
}

/** The checks a connection test ran — the settings, then the live probe. */
export function ConnectionTestTable({ result }: { result: ConnectionTest }): JSX.Element {
  const failed = result.checks.filter((c) => c.status === 'fail').length
  const probe = result.live_probe ?? []
  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <p className={`text-xs ${failed === 0 ? 'text-success' : 'text-destructive'}`}>
          {failed === 0
            ? 'Every check passed.'
            : `${failed} of ${result.checks.length} checks failed.`}
        </p>
        <CheckRows checks={result.checks} />
      </div>
      {probe.length > 0 && (
        <div className="space-y-1">
          <h3 className="text-xs font-medium text-foreground">Live probe</h3>
          <p
            className={`text-xs ${
              result.live_probe_ok === true
                ? 'text-success'
                : result.live_probe_ok === false
                  ? 'text-destructive'
                  : 'text-muted-foreground'
            }`}
          >
            {liveProbeSummary(result)}
          </p>
          <CheckRows checks={probe} />
        </div>
      )}
    </div>
  )
}

function ago(iso: string | null | undefined, now: Date): string {
  return iso ? relativeTime(iso, now) : '—'
}

/** The live probe's last verdict as a badge: pass, fail, or not set up. */
function ProbeBadge({ probe }: { probe: ConnectedApp['live_probe'] }): JSX.Element {
  const [label, style] =
    probe.ok === true
      ? ['pass', STATUS_STYLE.pass]
      : probe.ok === false
        ? ['fail', STATUS_STYLE.fail]
        : [probe.at ? 'not set up' : 'not run', STATUS_STYLE.skip]
  return (
    <span className={`inline-block rounded border px-1.5 text-[10px] uppercase ${style}`}>{label}</span>
  )
}

/**
 * Every site that lets an agent act as its visitors, and whether that works:
 * canopy's live probe (a real grant for the site's probe user, every 30 minutes)
 * beside what real visitors' traffic says. A site that stops working shows here
 * before a visitor finds out.
 */
export function SiteHealthTable({
  apps,
  now = new Date(),
}: {
  apps: readonly ConnectedApp[]
  now?: Date
}): JSX.Element | null {
  const rows = apps.filter((a) => a.issues_host_grants && !a.revoked)
  if (rows.length === 0) return null
  return (
    <section className="space-y-2">
      <h2 className="text-sm font-medium text-foreground">Acting as visitors</h2>
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="border-b border-border text-left text-muted-foreground">
            <th className="py-1 pr-2 font-normal">Site</th>
            <th className="py-1 pr-2 font-normal">Live probe</th>
            <th className="py-1 pr-2 font-normal">Probed</th>
            <th className="py-1 pr-2 font-normal">Last grant</th>
            <th className="py-1 pr-2 font-normal">Last call</th>
            <th className="py-1 pr-2 text-right font-normal">Refusals 24h</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((app) => (
            <tr key={app.id} className="border-b border-border align-top last:border-0">
              <td className="py-1 pr-2 text-foreground">{app.name}</td>
              <td className="py-1 pr-2">
                <ProbeBadge probe={app.live_probe} />
                {app.live_probe.ok !== true && app.live_probe.step_label && (
                  <span
                    className="ml-2 text-foreground-secondary"
                    title={app.live_probe.reason}
                  >
                    {app.live_probe.ok === false ? app.live_probe.step_label : app.live_probe.reason}
                  </span>
                )}
              </td>
              <td className="py-1 pr-2 text-muted-foreground">{ago(app.live_probe.at, now)}</td>
              <td className="py-1 pr-2 text-muted-foreground">{ago(app.traffic.last_redeemed_at, now)}</td>
              <td className="py-1 pr-2 text-muted-foreground">{ago(app.traffic.last_site_call_at, now)}</td>
              <td
                className={`py-1 pr-2 text-right tabular-nums ${
                  app.traffic.refusals_24h > 0 ? 'text-warning' : 'text-muted-foreground'
                }`}
              >
                {app.traffic.refusals_24h}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

/** "Test connection": runs canopy's conformance checks against this site's
 *  settings, from canopy's server, and shows the table. */
export function ConnectionTester({ slug, app }: { slug: string; app: ConnectedApp }): JSX.Element {
  const [result, setResult] = useState<ConnectionTest | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onTest() {
    setRunning(true)
    setError(null)
    try {
      setResult(await testConnectedApp(slug, app.id))
    } catch (e) {
      setResult(null)
      setError(e instanceof Error ? e.message : 'Could not test the connection.')
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="space-y-2 border-t border-border pt-2">
      <div className="flex items-center gap-3">
        <Button size="sm" variant="outline" disabled={running} onClick={() => void onTest()}>
          {running ? 'Testing…' : 'Test connection'}
        </Button>
        <span className="text-xs text-muted-foreground">
          Reads its keys and discovery documents, asks whether it accepts canopy, then runs the live
          probe — from canopy&apos;s server.
        </span>
      </div>
      {error && <p className="text-xs text-destructive">{error}</p>}
      {result && <ConnectionTestTable result={result} />}
    </div>
  )
}

export function ConnectedAppsPage(): JSX.Element | null {
  const { workspace: slug } = useParams()
  const { workspaces } = useWorkspace()
  const isOwner = workspaces.find((w) => w.slug === slug)?.role === 'owner'

  const [apps, setApps] = useState<ConnectedApp[] | null>(null)
  const [agents, setAgents] = useState<AgentOut[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
      setApps(await listConnectedApps(slug))
      setLoadError(null)
    } catch (e) {
      setLoadError(
        e instanceof WorkspaceApiError && e.status === 403
          ? 'Only a workspace owner can manage connected sites.'
          : e instanceof Error
            ? e.message
            : 'Could not load connected sites.',
      )
    }
  }, [slug])

  useEffect(() => {
    void reload()
    void listAgents({ limit: 100 })
      .then((p) => setAgents(p.items))
      .catch(() => setAgents([]))
  }, [reload])


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
    await run(async () => {
      await connectApp(slug!, {
        name: name.trim(),
        origins: parseOrigins(origins),
        show_on_canopy_pages: showHere,
        agents: picked,
        public_keys: parseKeys(signingKey),
        jwks_url: jwksUrl.trim(),
        host_issuer: hostIssuer.trim(),
        host_mcp_resource: hostResource.trim(),
      })
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
      <WorkbenchSubHeader title="Connected sites" count={apps?.length} />
      <p className="-mt-4 text-sm text-muted-foreground">
        Websites allowed to host one of this workspace's agents.
      </p>

      {!isOwner && (
        <p className="text-sm text-muted-foreground">
          Only a workspace owner can change these.
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
            <div key={app.id} className="rounded-lg border border-border bg-card p-4 space-y-2">
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
                </div>
                {isOwner && !app.revoked && (
                  // This workspace's own registration. Another workspace using
                  // the same system registered it separately.
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy}
                    onClick={() => void run(() => disconnectApp(slug, app.id))}
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
                    void run(() => updateConnectedApp(slug, app.id, { agents: slugs }))
                  }
                />
              )}
              {isOwner && !app.revoked && (
                <HostGrantEditor
                  app={app}
                  busy={busy}
                  onSave={(issuer, resource) =>
                    void run(() =>
                      updateConnectedApp(slug, app.id, {
                        host_issuer: issuer,
                        host_mcp_resource: resource,
                      }),
                    )
                  }
                />
              )}
              {isOwner && !app.revoked && <ConnectionTester slug={slug} app={app} />}
            </div>
          ))}
      </section>

      {isOwner && (
        <form onSubmit={onConnect} className="rounded-lg border border-border bg-card p-4 space-y-3">
          <h2 className="text-sm font-medium text-foreground">Connect another site</h2>

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
            Connect
          </Button>
        </form>
      )}
    </div>
  )
}
