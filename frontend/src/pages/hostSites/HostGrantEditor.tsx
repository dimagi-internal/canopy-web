import { useState, type JSX } from 'react'
import { Button } from 'canopy-ui/ui'
import { type AgentOut } from '@/api/agents'
import { runnerFlagLabel } from '@/components/chat/runnerEligibility'
import { type HostSite } from '@/api/hostSites'

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
  app: HostSite
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
export function RunnerRequirements({ app }: { app: Pick<HostSite, 'runner_requirements'> }): JSX.Element | null {
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
  app: HostSite
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

/**
 * Reply push: where canopy POSTs the agent's replies in the conversations this site relays over
 * its own channel (a relay never polls). One webhook per site, whatever its surfaces: only relay
 * conversations are pushed, and the site routes each reply by its conversation. It receives
 * those conversations' words, so changing the URL asks for the person's passkey. A site may
 * declare its own webhook in its signed `site.json` instead (shown under what the site asks); one
 * set here wins over it.
 */
export function ReplyPushEditor({
  app,
  busy,
  onSave,
}: {
  app: Pick<HostSite, 'reply_webhook_url' | 'reply_mode'> & Partial<Pick<HostSite, 'reply_webhook_set_by'>>
  busy: boolean
  onSave: (url: string, mode: 'final' | 'stream') => void
}): JSX.Element {
  const savedUrl = app.reply_webhook_url ?? ''
  const savedMode = app.reply_mode === 'stream' ? 'stream' : 'final'
  const [url, setUrl] = useState(savedUrl)
  const [mode, setMode] = useState<'final' | 'stream'>(savedMode)
  const dirty = url.trim() !== savedUrl || mode !== savedMode
  return (
    <div className="space-y-2 border-t border-border pt-2">
      <p className="text-xs text-muted-foreground">
        {savedUrl
          ? `Set by ${app.reply_webhook_set_by || 'a person'}: canopy pushes the agent's replies in this site's relayed conversations to it, over any webhook the site declares.`
          : "No webhook set here. canopy uses the one the site declares in its site.json, when it is on one of the site's origins; without one, a relay has to poll for replies."}
      </p>
      <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
        <label className="block space-y-1">
          <span className="text-xs text-foreground-secondary">Reply webhook URL (optional)</span>
          <input
            aria-label="Reply webhook URL"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://labs.connect.dimagi.com/messaging/canopy/replies/"
            className="w-full rounded-md border border-input bg-input px-3 py-2 font-mono text-[11px] text-foreground"
          />
        </label>
        <label className="block space-y-1">
          <span className="text-xs text-foreground-secondary">Reply mode</span>
          <select
            aria-label="Reply mode"
            value={mode}
            onChange={(e) => setMode(e.target.value === 'stream' ? 'stream' : 'final')}
            className="rounded border border-input bg-input px-2 py-2 text-xs text-foreground"
          >
            <option value="final">final: one post when a turn ends</option>
            <option value="stream">stream: each reply as it lands</option>
          </select>
        </label>
      </div>
      <span className="block text-xs text-muted-foreground">
        Public https, the URL the site verifies replies against. Changing it asks for your passkey.
      </span>
      {dirty && (
        <Button size="sm" disabled={busy} onClick={() => onSave(url.trim(), mode)}>
          Save
        </Button>
      )}
    </div>
  )
}
