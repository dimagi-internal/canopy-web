import { useEffect, useState, type JSX } from 'react'
import { runnerFlagLabel } from '@/components/chat/runnerEligibility'
import { getConnectedAppNeeds, type ConnectedApp, type SiteNeeds, type AgentFit } from '@/api/connectedApps'

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
