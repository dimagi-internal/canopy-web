import { useCallback, useEffect, useState, type FormEvent, type JSX } from 'react'
import { useParams } from 'react-router-dom'
import {
  configureConnectionApp,
  listConnectionApps,
  removeConnectionApp,
  type ConnectionAppOut,
} from '@/api/connections'
import { ApiError } from '@/api/problem'
import { Button, CopyButton, Input } from 'canopy-ui/ui'

/**
 * The OAuth clients ("apps") people and agents in this workspace connect with,
 * one card per provider the API lists. Workspace admins only (the server gates
 * it on `integrations`).
 *
 * Save and Remove need a fresh passkey: the API client's step-up middleware
 * prompts and retries, so nothing here knows. The client secret is write-only —
 * the API never returns it (`has_secret` says whether one is saved), and the
 * field is cleared once a save lands.
 */
export function ConnectionAppsPanel({ workspace }: { workspace: string }): JSX.Element {
  const [apps, setApps] = useState<ConnectionAppOut[] | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    listConnectionApps(workspace)
      .then((a) => { setApps(a); setError('') })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not read the connection apps.'))
  }, [workspace])
  useEffect(load, [load])

  return (
    <div className="flex max-w-3xl flex-col gap-5" data-testid="connection-apps">
      <section>
        <h2 className="text-[15px] font-semibold text-foreground">Connections</h2>
        <p className="mt-1 text-[13px] text-foreground-secondary">
          The OAuth apps people and agents in this workspace connect their accounts with.
        </p>
      </section>
      {error && <p className="text-[13px] text-destructive">{error}</p>}
      {apps?.map((a) => (
        <AppCard
          key={`${a.provider}:${a.updated_at ?? ''}`}
          workspace={workspace}
          app={a}
          onChanged={load}
        />
      ))}
    </div>
  )
}

/** The workspace settings section: the panel for the URL's workspace. */
export function WorkspaceConnectionAppsSection(): JSX.Element {
  const { workspace = '' } = useParams()
  return <ConnectionAppsPanel workspace={workspace} />
}

/** Where the callback URL is registered, per provider (plain words, one line). */
const REGISTER_AT: Record<string, string> = {
  salesforce: 'your Salesforce External Client App',
  google: 'your Google OAuth client (Web application)',
}

/** One provider-specific caution, shown muted under the callback URL. */
const HELP: Record<string, string> = {
  google:
    'Use an OAuth client whose consent screen is published (In production). An app left in Testing loses its connections after 7 days.',
}

function plural(n: number, one: string): string {
  return `${n} ${one}${n === 1 ? '' : 's'}`
}

function initialOptions(app: ConnectionAppOut): Record<string, string> {
  const saved = app.options ?? {}
  const out: Record<string, string> = {}
  for (const f of app.option_fields ?? []) {
    const v = saved[f.name]
    out[f.name] = v === undefined || v === null ? '' : String(v)
  }
  return out
}

function AppCard({
  workspace,
  app,
  onChanged,
}: {
  workspace: string
  app: ConnectionAppOut
  onChanged: () => void
}) {
  const [clientId, setClientId] = useState(app.client_id)
  const [secret, setSecret] = useState('')
  const [options, setOptions] = useState<Record<string, string>>(() => initialOptions(app))
  const [scopes, setScopes] = useState<string[]>(() => [...(app.scopes ?? [])])
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null)

  const p = app.provider
  const fields = app.option_fields ?? []
  const choices = app.scope_choices ?? []
  const hasLoginUrl = fields.some((f) => f.name === 'login_url')
  const id = (what: string) => `connection-app-${p}-${what}`

  function toggle(scope: string, on: boolean) {
    setScopes((s) => (on ? [...s.filter((x) => x !== scope), scope] : s.filter((x) => x !== scope)))
  }

  async function save(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setNote(null)
    try {
      await configureConnectionApp(workspace, p, {
        client_id: clientId.trim(),
        client_secret: secret,
        options,
        // The full list: the server replaces the saved scopes with it.
        scopes: choices.map((c) => c.scope).filter((s) => scopes.includes(s)),
      })
      setSecret('') // never keep a secret in component state once it has landed
      setNote({ tone: 'ok', text: 'Saved.' })
      onChanged()
    } catch (err) {
      const text = err instanceof ApiError && err.code === 'app_locked'
        ? `Disconnect its ${plural(app.connection_count, 'connection')} (Remove) before changing the client ID${hasLoginUrl ? ' or login URL' : ''}.`
        : err instanceof Error ? err.message : 'Could not save the app.'
      setNote({ tone: 'error', text })
    } finally {
      setBusy(false)
    }
  }

  async function remove() {
    const n = app.connection_count
    if (!window.confirm(`Remove the ${app.title} app? This disconnects ${plural(n, 'connection')}.`)) return
    setBusy(true)
    setNote(null)
    try {
      await removeConnectionApp(workspace, p)
      onChanged()
    } catch (err) {
      setNote({ tone: 'error', text: err instanceof Error ? err.message : 'Could not remove the app.' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      onSubmit={save}
      className="space-y-4 rounded-xl border border-border bg-card p-5"
      data-testid={`connection-app-${p}`}
    >
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-foreground">{app.title}</h3>
        <span className={`text-[12px] ${app.configured ? 'text-success' : 'text-muted-foreground'}`}>
          {app.configured ? 'Set up' : 'Not set up'}
        </span>
      </div>

      <div className="space-y-1">
        <div className="flex items-center gap-2">
          <code className="min-w-0 flex-1 truncate rounded border border-border bg-muted/40 px-2 py-1 text-[12px]">
            {app.callback_url}
          </code>
          <CopyButton text={app.callback_url} variant="outline" />
        </div>
        <p className="text-[12px] text-foreground-secondary">
          Register this as the redirect URI in {REGISTER_AT[p] ?? `your ${app.title} OAuth client`}.
        </p>
        {HELP[p] && <p className="text-[12px] text-muted-foreground">{HELP[p]}</p>}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1 text-[12px] text-muted-foreground" htmlFor={id('client-id')}>
          <span>Client ID</span>
          <Input id={id('client-id')} value={clientId} autoComplete="off" onChange={(e) => setClientId(e.target.value)} />
        </label>
        <label className="space-y-1 text-[12px] text-muted-foreground" htmlFor={id('client-secret')}>
          <span>Client secret</span>
          <Input
            id={id('client-secret')}
            type="password"
            value={secret}
            autoComplete="new-password"
            placeholder={app.has_secret ? 'Saved — leave blank to keep' : ''}
            onChange={(e) => setSecret(e.target.value)}
          />
        </label>
        {fields.map((f) => (
          <label key={f.name} className="space-y-1 text-[12px] text-muted-foreground" htmlFor={id(f.name)}>
            <span>{f.label}{f.required ? '' : ' (optional)'}</span>
            <Input
              id={id(f.name)}
              type={f.kind === 'url' ? 'url' : f.kind === 'int' ? 'number' : 'text'}
              value={options[f.name] ?? ''}
              placeholder={f.default === undefined || f.default === null ? '' : String(f.default)}
              onChange={(e) => setOptions((o) => ({ ...o, [f.name]: e.target.value }))}
            />
            {f.help && <span className="block text-[11px]">{f.help}</span>}
          </label>
        ))}
      </div>

      {choices.length > 0 && (
        <fieldset className="space-y-1.5">
          <legend className="text-[12px] text-muted-foreground">Permissions to ask for</legend>
          {choices.map((c) => (
            <label key={c.scope} className="flex items-center gap-2 text-[13px] text-foreground">
              <input
                type="checkbox"
                checked={scopes.includes(c.scope)}
                onChange={(e) => toggle(c.scope, e.target.checked)}
              />
              {c.label}
            </label>
          ))}
        </fieldset>
      )}

      {note && (
        <p role="status" className={`text-[13px] ${note.tone === 'ok' ? 'text-success' : 'text-destructive'}`}>
          {note.text}
        </p>
      )}

      <div className="flex items-center gap-2">
        <Button type="submit" size="sm" disabled={busy || !clientId.trim()}>
          {busy ? 'Saving…' : 'Save'}
        </Button>
        {(app.configured || app.client_id) && (
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void remove()}>
            Remove
          </Button>
        )}
      </div>
    </form>
  )
}
