import { useCallback, useEffect, useState, type FormEvent, type JSX } from 'react'
import { useParams } from 'react-router-dom'
import { Button, Input } from 'canopy-ui/ui'
import { WorkbenchSubHeader } from 'canopy-ui'

import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { listAgents, type AgentOut } from '@/api/agents'
import {
  connectApp,
  disconnectApp,
  listConnectedApps,
  updateConnectedApp,
  type ConnectedApp,
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
      })
      setName('')
      setOrigins('')
      setPicked([])
      setSigningKey('')
      setShowHere(false)
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
