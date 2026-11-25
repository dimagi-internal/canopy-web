import { useEffect, useState, type JSX } from 'react'
import { runnerFlagLabel } from '@/components/chat/runnerEligibility'
import {
  getHostSiteNeeds,
  type HostSite,
  type SiteNeeds,
  type AgentFit,
  type SiteSurface,
  type ReplyPush,
} from '@/api/hostSites'
import { Td, Th, Tr } from '@/components/DataTable'

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
    req.handles_sensitive && 'an agent that handles sensitive data',
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
          <Tr rule="bottom" head="plain">
            <Th density="compact-gutter" weight="normal">Agent</Th>
            <Th density="compact-gutter" weight="normal">Offered on the site</Th>
            <Th density="compact-gutter" weight="normal">Sensitive data</Th>
            <Th density="compact-gutter" weight="normal">Ready</Th>
            <Th density="compact-gutter" weight="normal">Can handle sensitive data</Th>
          </Tr>
        </thead>
        <tbody>
          {needs.agents.map((a) => (
            <Tr
              rule="bottom-last"
              top
              key={a.slug}
              data-attention={needsAttention(a) || undefined}
              className={needsAttention(a) ? 'bg-destructive/5' : undefined}
            >
              <Td density="compact-gutter" tone="foreground">
                <a href={a.url} className="hover:underline">
                  {a.name}
                </a>
              </Td>
              <Td density="compact-gutter"><Tick on={a.offered} /></Td>
              <Td density="compact-gutter"><Tick on={a.sensitive_data} /></Td>
              <Td density="compact-gutter"><Tick on={a.ready} /></Td>
              <Td density="compact-gutter"><CanHandle agent={a} /></Td>
            </Tr>
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
      {needs.surfaces.length > 0 && <SurfacesTable surfaces={needs.surfaces} />}
      {needs.reply_push && <ReplyPushSource push={needs.reply_push} />}
      {needs.pages.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            Pages this site draws agents on ({needs.pages.length}) — the site&apos;s own description; canopy
            shows it for reference
          </summary>
          <table className="mt-1 w-full border-collapse">
            <thead>
              <Tr rule="bottom" head="plain">
                <Th density="compact-gutter" weight="normal">Page</Th>
                <Th density="compact-gutter" weight="normal">Key</Th>
                <Th density="compact-gutter" weight="normal">Scopes</Th>
              </Tr>
            </thead>
            <tbody>
              {needs.pages.map((p) => (
                <Tr rule="bottom-last" top key={p.key}>
                  <Td density="compact-gutter" tone="foreground">{p.title || p.key}</Td>
                  <Td density="compact-gutter" tone="muted" mono>{p.key}</Td>
                  <Td density="compact-gutter" tone="muted" mono>{p.scopes.join(' ')}</Td>
                </Tr>
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

/**
 * The parts of the site that serve different people, as its `site.json` declares them. A contact
 * (one of the site's own people, with no canopy account) holds a conversation only on a surface
 * that holds no sensitive data, through an agent whose host-site entry names it; its tools are
 * called at that surface's MCP server, never the site's own.
 */
export function SurfacesTable({ surfaces }: { surfaces: readonly SiteSurface[] }): JSX.Element {
  return (
    <div className="space-y-1">
      <h4 className="text-xs text-foreground">Surfaces</h4>
      <table className="w-full border-collapse text-xs">
        <thead>
          <Tr rule="bottom" head="plain">
            <Th density="compact-gutter" weight="normal">Surface</Th>
            <Th density="compact-gutter" weight="normal">Sensitive data</Th>
            <Th density="compact-gutter" weight="normal">MCP server</Th>
            <Th density="compact-gutter" weight="normal">People&apos;s records</Th>
            <Th density="compact-gutter" weight="normal">Agents serving it</Th>
          </Tr>
        </thead>
        <tbody>
          {surfaces.map((s) => (
            <Tr rule="bottom-last" top key={s.name}>
              <Td density="compact-gutter" tone="foreground">
                {s.title || s.name} <span className="text-muted-foreground">({s.name})</span>
              </Td>
              <Td density="compact-gutter"><Tick on={s.sensitive} /></Td>
              <Td density="compact-gutter" tone="muted" mono>{s.mcp_resource || '—'}</Td>
              <Td density="compact-gutter" tone="muted" mono>
                {s.person_scheme ? `${s.person_scheme}://` : '—'}
              </Td>
              <Td density="compact-gutter" tone="muted">
                {s.agents.length > 0 ? s.agents.join(', ') : 'none yet'}
              </Td>
            </Tr>
          ))}
        </tbody>
      </table>
      <p className="text-muted-foreground text-xs">
        An agent serves a surface when its host site entry for this site names it. The site&apos;s
        own contacts reach it only on a surface without sensitive data.
      </p>
    </div>
  )
}

/**
 * Where canopy pushes the agent's replies in the conversations this site relays, and on whose
 * word: declared by the site in its signed `site.json` (adopted only on one of the site's
 * registered origins), or set by a person on this page, which wins.
 */
export function ReplyPushSource({ push }: { push: ReplyPush }): JSX.Element | null {
  if (!push.url && !push.declared_url) return null
  return (
    <p className="text-xs text-muted-foreground">
      Reply webhook:{' '}
      {push.url ? (
        <>
          <span className="font-mono text-foreground">{push.url}</span> ({push.mode}),{' '}
          {push.source === 'site' ? 'declared by the site' : `set by ${push.set_by || 'a person'}`}
          {push.source === 'person' && push.declared_url && push.declared_url !== push.url && (
            <> — it wins over the one the site declares ({push.declared_url})</>
          )}
          .
        </>
      ) : (
        <>
          the site declares <span className="font-mono text-foreground">{push.declared_url}</span>, which canopy
          does not use: <span className="text-warning">{push.declared_reason}</span>.
        </>
      )}
    </p>
  )
}

/** Loads and shows `SiteNeedsTable` for one site. */
export function SiteNeedsSection({ slug, app }: { slug: string; app: HostSite }): JSX.Element {
  const [needs, setNeeds] = useState<SiteNeeds | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    getHostSiteNeeds(slug, app.id)
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
