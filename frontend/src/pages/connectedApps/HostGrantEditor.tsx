import { useState, type JSX } from 'react'
import { Button } from 'canopy-ui/ui'
import { type AgentOut } from '@/api/agents'
import { runnerFlagLabel } from '@/components/chat/runnerEligibility'
import { type ConnectedApp } from '@/api/connectedApps'

export function AgentPicker({
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
export function DefaultAgentPicker({
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
export function HostGrantFields({
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

export function HostGrantEditor({
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
