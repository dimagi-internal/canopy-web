import { useCallback, useEffect, useState, type FormEvent, type JSX } from 'react'
import { useParams } from 'react-router-dom'
import { Button, Input } from 'canopy-ui/ui'
import { WorkbenchSubHeader } from 'canopy-ui'

import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { useAuth } from '@/auth/AuthProvider'
import { listAgents, type AgentOut } from '@/api/agents'
import {
  connectApp,
  disconnectApp,
  enableSelfWidget,
  listConnectedApps,
  rotateSecret,
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

/** The origin of the page you are on, which is the value the self-widget needs
 *  and the likeliest thing someone wants for a first connection. */
export function currentOrigin(): string {
  return typeof window === 'undefined' ? '' : window.location.origin
}

function Secret({ value, onDone }: { value: string; onDone: () => void }): JSX.Element {
  const [copied, setCopied] = useState(false)
  return (
    <div className="rounded-lg border border-warning/40 bg-warning/10 p-4 space-y-2">
      <p className="text-sm text-foreground">
        Copy this now — canopy keeps only a hash of it, so it cannot be shown again.
      </p>
      <div className="flex items-center gap-2">
        <code className="flex-1 truncate rounded bg-input px-2 py-1 font-mono text-xs text-foreground">
          {value}
        </code>
        <Button
          size="sm"
          onClick={() => {
            void navigator.clipboard?.writeText(value)
            setCopied(true)
          }}
        >
          {copied ? 'Copied' : 'Copy'}
        </Button>
        <Button size="sm" variant="ghost" onClick={onDone}>
          Done
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Your site sends it as <code>Authorization: Bearer …</code> when it exchanges a
        token for a signed-in user. A site that only hosts the widget on canopy's own
        pages never needs it.
      </p>
    </div>
  )
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
  const auth = useAuth()
  const myDomain =
    auth.status === 'authenticated' ? auth.user.email.split('@').pop()?.toLowerCase() ?? '' : ''

  const [apps, setApps] = useState<ConnectedApp[] | null>(null)
  const [agents, setAgents] = useState<AgentOut[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [secret, setSecret] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [name, setName] = useState('')
  const [origins, setOrigins] = useState('')
  const [vouch, setVouch] = useState(false)
  const [picked, setPicked] = useState<string[]>([])

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

  const selfApp = apps?.find((a) => a.is_self && !a.revoked) ?? null

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
      const created = await connectApp(slug!, {
        name: name.trim(),
        origins: parseOrigins(origins),
        // A domain is never typed. The server grants the acting owner's own
        // domain and refuses any other, so the question a person can answer
        // ("does your site sign users in?") is the one being asked.
        delegation_domains: vouch && myDomain ? [myDomain] : [],
        agents: picked,
      })
      setSecret(created.secret)
      setName('')
      setOrigins('')
      setPicked([])
    })
  }

  if (!slug) return null

  return (
    <div className="space-y-6">
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
      {secret && <Secret value={secret} onDone={() => setSecret(null)} />}

      {/* The one-click case, first, because it is the one most people want and
          because every input it would otherwise ask for is a fact about canopy
          rather than a decision. */}
      {isOwner && (
        <section className="rounded-lg border border-border bg-card p-4 space-y-3">
          <div>
            <h2 className="text-sm font-medium text-foreground">canopy's own pages</h2>
            <p className="text-xs text-muted-foreground">
              Put the agent panel on canopy itself, so you can ask about the page you
              are looking at. Uses {currentOrigin()}.
            </p>
          </div>
          {selfApp ? (
            <div className="space-y-2">
              <p className="text-xs text-success">
                On — offering{' '}
                {selfApp.agents.length
                  ? selfApp.agents.map((a) => a.name).join(', ')
                  : 'no agents yet, so the panel will have nothing to show'}
                .
              </p>
              <AgentPicker
                agents={agents}
                selected={selfApp.agents.map((a) => a.slug)}
                onChange={(slugs) =>
                  void run(() => updateConnectedApp(slug, selfApp.id, { agents: slugs }))
                }
              />
            </div>
          ) : (
            <Button
              disabled={busy}
              onClick={() => void run(() => enableSelfWidget(slug, picked))}
            >
              Turn on the agent panel here
            </Button>
          )}
        </section>
      )}

      {/* Everything already connected. */}
      <section className="space-y-3">
        <h2 className="text-sm font-medium text-foreground">Other sites</h2>
        {apps === null && !loadError && (
          <p className="text-sm text-muted-foreground">Loading…</p>
        )}
        {apps?.filter((a) => !a.is_self).length === 0 && (
          <p className="text-sm text-muted-foreground">No other sites connected yet.</p>
        )}
        {apps
          ?.filter((a) => !a.is_self)
          .map((app) => (
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
                </div>
                {isOwner && !app.revoked && (
                  <div className="flex shrink-0 gap-2">
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy}
                      onClick={() =>
                        void run(async () => setSecret(await rotateSecret(slug, app.id)))
                      }
                    >
                      New secret
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy}
                      onClick={() => void run(() => disconnectApp(slug, app.id))}
                    >
                      Disconnect
                    </Button>
                  </div>
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
              Letters, digits, hyphens. Your site passes this to{' '}
              <code>canopy.init</code>, so it has to match exactly.
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
              One per line. Include every environment — staging and local too. Scheme,
              host and port only; no path, no wildcard. This list is what stops any
              other site framing your agent, so leaving it empty serves nothing at all.
            </span>
          </label>

          <div className="space-y-1">
            <span className="text-xs text-foreground-secondary">Agents it may offer</span>
            <AgentPicker agents={agents} selected={picked} onChange={setPicked} />
          </div>

          {/* Asked as a question about the site, not as a domain field. The
              server only ever grants the acting owner's own domain and refuses
              any other, so there is nothing here to get wrong — and nothing a
              typo could widen. */}
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={vouch}
              onChange={(e) => setVouch(e.target.checked)}
              className="mt-1"
            />
            <span className="text-xs text-foreground-secondary">
              This site has its own sign-in, and should show each visitor their own
              canopy agent.
              <span className="block text-muted-foreground">
                Grants it the right to vouch for <strong>{myDomain || 'your domain'}</strong>{' '}
                users — anyone holding its secret can then act as any canopy user with
                that email domain. Leave this off for a site that only embeds canopy's
                own pages.
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
