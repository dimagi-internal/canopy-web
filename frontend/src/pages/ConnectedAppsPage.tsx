import { useCallback, useEffect, useState, type FormEvent, type JSX } from 'react'
import { useLocation, useParams } from 'react-router-dom'
import { Button, Input } from 'canopy-ui/ui'
import { WorkbenchSubHeader } from 'canopy-ui'

import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { listAgents, type AgentOut } from '@/api/agents'
import { relativeAge } from '@/lib/relativeAge'
import { runnerFlagLabel } from '@/components/chat/runnerEligibility'
import {
  connectApp,
  disconnectApp,
  discoverSite,
  getConnectedAppNeeds,
  listConnectedApps,
  testConnectedApp,
  updateConnectedApp,
  type ConnectedApp,
  type ConnectionCheck,
  type ConnectionTest,
  type SiteDiscovery,
  type SiteFacts,
  type SiteNeeds,
  type AgentFit,
} from '@/api/connectedApps'
import { WorkspaceApiError } from '@/api/workspaces'
import { roleAllows } from '@/lib/workspaceRoles'
import { toneClasses } from 'canopy-ui/lib'

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
 * The agent this site's panel opens on when neither the page nor the visitor
 * chose one. A visitor's own default (set from the panel) and a page that
 * chose its agent both win over it.
 */
function DefaultAgentPicker({
  app,
  busy,
  onChange,
}: {
  app: ConnectedApp
  busy: boolean
  onChange: (agent: string) => void
}): JSX.Element {
  return (
    <label className="flex items-center gap-2 text-xs text-foreground-secondary">
      Opens on
      <select
        aria-label="Default agent"
        value={app.default_agent ?? ''}
        disabled={busy}
        onChange={(e) => onChange(e.target.value)}
        className="rounded border border-input bg-input px-2 py-1 text-xs text-foreground"
      >
        <option value="">No default (the visitor picks)</option>
        {app.agents.map((a) => (
          <option key={a.slug} value={a.slug}>
            {a.name}
          </option>
        ))}
      </select>
      <span className="text-muted-foreground">unless the page or the visitor chose another</span>
    </label>
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

/** What the host last said its visitors' runners must guarantee. Display only:
 *  the signed claim on each arrival is the authority. */
export function RunnerRequirements({ app }: { app: Pick<ConnectedApp, 'runner_requirements'> }): JSX.Element | null {
  const reqs = app.runner_requirements ?? []
  if (reqs.length === 0) return null
  return (
    <p className="text-xs text-info" data-testid="runner-requirements">
      Requires {reqs.map(runnerFlagLabel).join(', ')} runners (declared by the host)
    </p>
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
      <RunnerRequirements app={app} />
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
  pass: toneClasses('success'),
  fail: toneClasses('destructive'),
  skip: toneClasses('neutral'),
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
  return iso ? relativeAge(iso, now, { seconds: false }) : '—'
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

function Tick({ on }: { on: boolean }): JSX.Element {
  return <span className={on ? 'text-foreground' : 'text-muted-foreground'}>{on ? 'yes' : '—'}</span>
}

/** Whether canopy should draw the owner's eye to this agent: only when the site
 *  offers it, lets it work with its sensitive data, and canopy says it cannot
 *  handle that data yet. Everything else is neutral. */
export function needsAttention(a: AgentFit): boolean {
  return a.offered && a.sensitive_data && a.can_handle_sensitive_data === false
}

function CanHandle({ agent }: { agent: AgentFit }): JSX.Element {
  if (agent.can_handle_sensitive_data == null) {
    return <span className="text-muted-foreground">not needed</span>
  }
  if (agent.can_handle_sensitive_data) return <span className="text-success">yes</span>
  return (
    <ul className={needsAttention(agent) ? 'text-destructive' : 'text-foreground'}>
      {agent.why_not.map((why) => (
        <li key={why.text}>
          {why.text}
          {why.url && (
            <>
              {' — '}
              <a href={why.url} className="underline underline-offset-2">
                fix in settings
              </a>
            </>
          )}
        </li>
      ))}
    </ul>
  )
}

/**
 * What the site asks of canopy's agents (its signed `site.json`, read beside its
 * `jwks.json`): the one question the owner can act on — can each agent the site
 * lets near its sensitive data handle it? The fit is computed from the same site
 * agents API the site reads (`/api/sites/agents`), so the two never disagree;
 * every "no" links to where in canopy to fix it. The site's pages and scopes are
 * its own description (it enforces them; canopy never tells an agent), shown
 * collapsed for reference.
 */
export function SiteNeedsTable({ needs }: { needs: SiteNeeds }): JSX.Element {
  if (!needs.available) {
    return (
      <p className="text-xs text-muted-foreground">
        What it asks: <span className="text-warning">not available</span> — {needs.reason}
      </p>
    )
  }
  const req = needs.requires
  const asks = [
    req.handles_sensitive && 'an agent capability that handles sensitive data',
    req.runner_flag && `running on a ${runnerFlagLabel(req.runner_flag)} runner`,
  ]
    .filter(Boolean)
    .join(', ')
  return (
    <div className="space-y-2">
      {needs.stale && (
        <p className="text-xs text-warning">
          Showing the last copy canopy could verify; refreshing failed — {needs.reason}
        </p>
      )}
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="border-b border-border text-left text-muted-foreground">
            <th className="py-1 pr-2 font-normal">Agent</th>
            <th className="py-1 pr-2 font-normal">Offered on the site</th>
            <th className="py-1 pr-2 font-normal">Sensitive data</th>
            <th className="py-1 pr-2 font-normal">Ready</th>
            <th className="py-1 pr-2 font-normal">Can handle sensitive data</th>
          </tr>
        </thead>
        <tbody>
          {needs.agents.map((a) => (
            <tr
              key={a.slug}
              data-attention={needsAttention(a) || undefined}
              className={`border-b border-border align-top last:border-0 ${needsAttention(a) ? 'bg-destructive/5' : ''}`}
            >
              <td className="py-1 pr-2 text-foreground">
                <a href={a.url} className="hover:underline">
                  {a.name}
                </a>
              </td>
              <td className="py-1 pr-2"><Tick on={a.offered} /></td>
              <td className="py-1 pr-2"><Tick on={a.sensitive_data} /></td>
              <td className="py-1 pr-2"><Tick on={a.ready} /></td>
              <td className="py-1 pr-2"><CanHandle agent={a} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-xs text-muted-foreground">
        {asks
          ? `For sensitive data, this site requires ${asks}.`
          : 'For sensitive data, this site names no requirement.'}
        {needs.uses_version && needs.uses_version !== needs.site_agents_version && (
          <span className="ml-1">
            The site chose its agents against an older site agents version; it picks up the current one on its
            next refresh.
          </span>
        )}
      </p>
      {needs.not_allowed.length > 0 && (
        <p className="text-xs text-muted-foreground">
          The site names {needs.not_allowed.join(', ')}, which this workspace does not allow on it
          — it can never show them.
        </p>
      )}
      {needs.pages.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            Pages this site draws agents on ({needs.pages.length}) — the site&apos;s own description; canopy
            shows it for reference
          </summary>
          <table className="mt-1 w-full border-collapse">
            <thead>
              <tr className="border-b border-border text-left text-muted-foreground">
                <th className="py-1 pr-2 font-normal">Page</th>
                <th className="py-1 pr-2 font-normal">Key</th>
                <th className="py-1 pr-2 font-normal">Scopes</th>
              </tr>
            </thead>
            <tbody>
              {needs.pages.map((p) => (
                <tr key={p.key} className="border-b border-border align-top last:border-0">
                  <td className="py-1 pr-2 text-foreground">{p.title || p.key}</td>
                  <td className="py-1 pr-2 font-mono text-muted-foreground">{p.key}</td>
                  <td className="py-1 pr-2 font-mono text-muted-foreground">{p.scopes.join(' ')}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1 text-muted-foreground">
            The site enforces these scopes itself. Agents never need to know a site&apos;s pages: they declare
            only <code>sites:</code> and <code>data: sensitive</code>.
          </p>
        </details>
      )}
    </div>
  )
}

/** Loads and shows `SiteNeedsTable` for one site. */
export function SiteNeedsSection({ slug, app }: { slug: string; app: ConnectedApp }): JSX.Element {
  const [needs, setNeeds] = useState<SiteNeeds | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    getConnectedAppNeeds(slug, app.id)
      .then((n) => live && setNeeds(n))
      .catch((e: unknown) => live && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      live = false
    }
  }, [slug, app.id, app.jwks_url])

  return (
    <div className="space-y-2 border-t border-border pt-2">
      <h3 className="text-xs font-medium text-foreground">What this site asks of agents</h3>
      {error && <p className="text-xs text-destructive">{error}</p>}
      {!needs && !error && <p className="text-xs text-muted-foreground">Loading…</p>}
      {needs && <SiteNeedsTable needs={needs} />}
    </div>
  )
}

const FACT_ROWS: { key: keyof SiteFacts; label: string }[] = [
  { key: 'origins', label: 'Site URLs' },
  { key: 'jwks_url', label: 'Signing keys URL' },
  { key: 'issuer', label: 'Sign-in issuer' },
  { key: 'mcp_resource', label: 'MCP server' },
]

function factText(facts: SiteFacts, key: keyof SiteFacts): string {
  const v = facts[key]
  return (typeof v === 'string' ? v : v.join(', ')) || '—'
}

/** How canopy believed the document, in words: an owner adopting keys should
 *  know when the keys come from the document itself. */
export function discoverySource(d: Pick<SiteDiscovery, 'url' | 'signed_by' | 'registered_keys_verify'>): string {
  const how =
    d.signed_by === 'registered_keys'
      ? 'signed with the keys canopy already has for this site'
      : d.registered_keys_verify === false
        ? 'NOT signed with the keys canopy has for this site — only with the keys it names; saving adopts those keys'
        : 'signed with the keys it names'
  return `Read from ${d.url}, ${how}.`
}

/** What the site says about itself, beside what canopy holds now (for a
 *  registered site). One row per fact; a changed row is marked. */
export function SiteFactsTable({ discovery }: { discovery: SiteDiscovery }): JSX.Element {
  const changed = new Set(discovery.changes ?? [])
  const current = discovery.current
  return (
    <table className="w-full border-collapse text-xs">
      <thead>
        <tr className="border-b border-border text-left text-muted-foreground">
          <th className="py-1 pr-2 font-normal">Setting</th>
          {current && <th className="py-1 pr-2 font-normal">In canopy now</th>}
          <th className="py-1 pr-2 font-normal">The site says</th>
        </tr>
      </thead>
      <tbody>
        {FACT_ROWS.map(({ key, label }) => (
          <tr key={key} className="border-b border-border align-top last:border-0">
            <td className="py-1 pr-2 text-foreground">
              {label}
              {changed.has(key) && <span className="ml-1 text-warning">changes</span>}
            </td>
            {current && (
              <td className="break-all py-1 pr-2 font-mono text-[11px] text-muted-foreground">
                {factText(current, key)}
              </td>
            )}
            <td className="break-all py-1 pr-2 font-mono text-[11px] text-foreground">
              {factText(discovery.site, key)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** The values to save when an owner adopts what a site declares. */
export type AdoptedFacts = {
  origins: string[]
  jwks_url: string
  host_issuer: string
  host_mcp_resource: string
}

export function adoptedFacts(site: SiteFacts): AdoptedFacts {
  return {
    origins: [...site.origins],
    jwks_url: site.jwks_url,
    host_issuer: site.issuer,
    host_mcp_resource: site.mcp_resource,
  }
}

/**
 * "Fill from the site's own documents" on a registered site: read its signed
 * `site.json`, show what it declares beside what canopy holds, and save only
 * when the owner says so — through the ordinary, audited update. A site never
 * changes its own trusted origins by publishing a document.
 */
export function FillFromSite({
  slug,
  app,
  busy,
  onSave,
}: {
  slug: string
  app: Pick<ConnectedApp, 'id'>
  busy: boolean
  onSave: (facts: AdoptedFacts) => void
}): JSX.Element {
  const [found, setFound] = useState<SiteDiscovery | null>(null)
  const [reading, setReading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onRead() {
    setReading(true)
    setError(null)
    try {
      setFound(await discoverSite(slug, { appId: app.id }))
    } catch (e) {
      setFound(null)
      setError(e instanceof Error ? e.message : "Could not read the site's own documents.")
    } finally {
      setReading(false)
    }
  }

  const changes = found?.changes ?? []
  return (
    <div className="space-y-2 border-t border-border pt-2">
      <div className="flex items-center gap-3">
        <Button size="sm" variant="outline" disabled={reading || busy} onClick={() => void onRead()}>
          {reading ? 'Reading…' : "Fill from the site's own documents"}
        </Button>
        <span className="text-xs text-muted-foreground">
          Reads the URLs, keys, issuer and MCP server the site publishes in its signed site.json. Nothing
          changes until you save.
        </span>
      </div>
      {error && <p className="text-xs text-destructive">{error}</p>}
      {found && (
        <div className="space-y-2">
          <p className={`text-xs ${found.signed_by === 'registered_keys' ? 'text-muted-foreground' : 'text-warning'}`}>
            {discoverySource(found)}
          </p>
          <SiteFactsTable discovery={found} />
          {changes.length === 0 ? (
            <p className="text-xs text-success">canopy already has what the site says.</p>
          ) : (
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                disabled={busy}
                onClick={() => {
                  onSave(adoptedFacts(found.site))
                  setFound(null)
                }}
              >
                Save these values
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setFound(null)}>
                Cancel
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * The first step of connecting a site: type its address, and canopy reads the
 * rest from the site's signed site.json into the form below for the owner to
 * check before connecting.
 */
export function SiteAddressFill({
  slug,
  onFill,
}: {
  slug: string
  onFill: (found: SiteDiscovery) => void
}): JSX.Element {
  const [address, setAddress] = useState('')
  const [reading, setReading] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function onRead() {
    setReading(true)
    setError(null)
    setNote(null)
    try {
      const found = await discoverSite(slug, { address: address.trim() })
      onFill(found)
      setNote(`${discoverySource(found)} Check the values below, then Connect.`)
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not read the site's own documents.")
    } finally {
      setReading(false)
    }
  }

  return (
    <div className="space-y-1">
      <span className="text-xs text-foreground-secondary">Site address</span>
      <div className="flex gap-2">
        <Input
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          placeholder="https://labs.connect.dimagi.com"
          aria-label="Site address"
        />
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={reading || !address.trim()}
          onClick={() => void onRead()}
        >
          {reading ? 'Reading…' : 'Fill from the site'}
        </Button>
      </div>
      <span className="block text-xs text-muted-foreground">
        A site on dimagi-canopy 0.16+ publishes its URLs, keys, issuer and MCP server; canopy fills them in
        for you to check.
      </span>
      {note && <p className="text-xs text-muted-foreground">{note}</p>}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
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
  const { hash } = useLocation()
  const { workspaces } = useWorkspace()
  // Two tiers (lib/workspaceRoles): an admin reads the sites and runs Test
  // connection; only an owner registers, changes or disconnects one — a
  // registered key vouches for visitors as members of this workspace.
  const myRole = workspaces.find((w) => w.slug === slug)?.role
  const isOwner = roleAllows(myRole, 'own')
  const canTest = roleAllows(myRole, 'integrations')

  const [apps, setApps] = useState<ConnectedApp[] | null>(null)
  const [agents, setAgents] = useState<AgentOut[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The site the form just connected. Its card lands in the list above, out
  // of sight of the button, so the form says so and links to it.
  const [connected, setConnected] = useState<ConnectedApp | null>(null)

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
          ? 'Only a workspace admin or owner can see connected sites.'
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
      const app = await connectApp(slug!, {
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
      <WorkbenchSubHeader title="Connected sites" count={apps?.length} />
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
              {isOwner && !app.revoked && app.agents.length > 1 && (
                <DefaultAgentPicker
                  app={app}
                  busy={busy}
                  onChange={(agent) =>
                    void run(() => updateConnectedApp(slug, app.id, { default_agent: agent }))
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
              {isOwner && !app.revoked && (
                <FillFromSite
                  slug={slug}
                  app={app}
                  busy={busy}
                  onSave={(facts) =>
                    void run(() =>
                      updateConnectedApp(slug, app.id, {
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
