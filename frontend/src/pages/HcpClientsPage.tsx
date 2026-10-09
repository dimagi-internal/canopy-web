import { useCallback, useEffect, useState, type FormEvent } from 'react'

import { MY_CATEGORIES, listHcpClients, registerHcpClient, updateHcpClient } from '../api/people'
import type { HcpClientRow } from '../api/people'

const ACTIONS = ['read', 'write'] as const

/** The HCP service's client registry (apps/contacts/hcp_clients_api.py): the apps
 *  canopy does not operate that may ask people, on a consent screen, for access to
 *  parts of their Human Context Protocol instance. Superusers only — the API
 *  refuses everyone else, and this page says so. */
export function HcpClientsPage() {
  const [clients, setClients] = useState<HcpClientRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [secret, setSecret] = useState<{ name: string; value: string } | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [operator, setOperator] = useState('')
  const [redirects, setRedirects] = useState('')
  const [webhook, setWebhook] = useState('')
  const [firstParty, setFirstParty] = useState(false)
  const [scopes, setScopes] = useState<Set<string>>(new Set())

  const load = useCallback(() => {
    listHcpClients()
      .then(setClients)
      .catch((e: Error) => setError(e.message))
  }, [])
  useEffect(load, [load])

  const toggleScope = (s: string) =>
    setScopes((prev) => {
      const next = new Set(prev)
      if (next.has(s)) next.delete(s)
      else next.add(s)
      return next
    })

  const onRegister = async (e: FormEvent) => {
    e.preventDefault()
    setBusy('register')
    try {
      const made = await registerHcpClient({
        name: name.trim(),
        operator: operator.trim(),
        description: '',
        redirect_uris: redirects.split(/\s+/).filter(Boolean),
        allowed_scopes: Array.from(scopes),
        first_party: firstParty,
        webhook_url: webhook.trim(),
      })
      if (made.webhook_secret) setSecret({ name: made.name, value: made.webhook_secret })
      setName('')
      setOperator('')
      setRedirects('')
      setWebhook('')
      setFirstParty(false)
      setScopes(new Set())
      load()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const onToggleDisabled = async (c: HcpClientRow) => {
    const disabling = c.disabled_at === null
    if (disabling && !confirm(`Disable ${c.name}? Every token it holds stops working at once.`)) return
    setBusy(c.client_id)
    try {
      await updateHcpClient(c.client_id, { disabled: disabling, rotate_webhook_secret: false })
      load()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(null)
    }
  }

  if (error) return <div className="mx-auto max-w-4xl px-4 py-8 text-destructive">{error}</div>
  if (clients === null) return <div className="p-6 text-muted-foreground">Loading…</div>

  return (
    <div className="mx-auto max-w-4xl space-y-6 px-4 py-8">
      <div>
        <h1 className="text-2xl font-semibold text-foreground">HCP service clients</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Apps canopy does not operate that may ask people for access to parts of what canopy knows
          about them. Each person decides on a consent screen, and can revoke it on their own page.
          The service is open to Dimagi accounts only for now.
        </p>
      </div>

      {secret && (
        <div role="status" className="rounded-lg border border-primary px-4 py-3 text-sm">
          <div className="font-semibold text-foreground">Webhook secret for {secret.name}</div>
          <p className="mt-1 text-muted-foreground">
            Shown once. Give it to the app's operator: it signs revocation notices (HCP-Signature).
          </p>
          <code className="mt-2 block break-all rounded bg-muted px-2 py-1 text-foreground">{secret.value}</code>
          <button className="mt-2 text-primary hover:underline" onClick={() => setSecret(null)}>
            I have stored it
          </button>
        </div>
      )}

      {clients.length === 0 ? (
        <p className="text-sm text-muted-foreground">No clients registered.</p>
      ) : (
        <ul className="divide-y divide-border rounded-lg border border-border text-sm">
          {clients.map((c) => (
            <li key={c.client_id} className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-start">
              <div className="min-w-0 flex-1">
                <div className="font-medium text-foreground">
                  {c.name} <span className="text-muted-foreground">· {c.operator}</span>
                  {c.disabled_at ? (
                    <span className="ml-2 rounded bg-destructive/10 px-1 text-xs text-destructive">disabled</span>
                  ) : null}
                </div>
                <div className="mt-0.5 break-all text-xs text-muted-foreground">
                  {c.client_id} · {c.active_grants} active grant{c.active_grants === 1 ? '' : 's'}
                </div>
                <div className="mt-0.5 break-all text-xs text-muted-foreground">
                  Returns to {c.redirect_uris.join(', ')}
                </div>
                <div className="mt-0.5 text-xs text-muted-foreground">May ask for {c.allowed_scopes.join(' ')}</div>
              </div>
              <button
                className="shrink-0 text-sm text-primary hover:underline disabled:opacity-50"
                disabled={busy !== null}
                onClick={() => onToggleDisabled(c)}
              >
                {c.disabled_at ? 'Re-enable' : 'Disable'}
              </button>
            </li>
          ))}
        </ul>
      )}

      <form onSubmit={onRegister} aria-label="Register a client" className="space-y-3 rounded-lg border border-border px-4 py-3">
        <div className="text-sm font-semibold text-foreground">Register an app</div>
        <div className="grid gap-2 sm:grid-cols-2">
          <input
            aria-label="Name"
            placeholder="Name people will see"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
          />
          <input
            aria-label="Operator"
            placeholder="Who runs it"
            value={operator}
            onChange={(e) => setOperator(e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
          />
        </div>
        <textarea
          aria-label="Redirect URIs"
          placeholder="Exact redirect URIs, one per line (https, or http to localhost)"
          value={redirects}
          onChange={(e) => setRedirects(e.target.value)}
          rows={2}
          className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm"
        />
        <fieldset className="text-sm">
          <legend className="text-muted-foreground">The most it may ask for</legend>
          <div className="mt-1 grid gap-1 sm:grid-cols-2">
            {MY_CATEGORIES.map((cat) =>
              ACTIONS.map((a) => {
                const s = `hcp:${cat.value}:${a}`
                return (
                  <label key={s} className="flex items-center gap-2">
                    <input type="checkbox" checked={scopes.has(s)} onChange={() => toggleScope(s)} />
                    {cat.label} — {a}
                  </label>
                )
              }),
            )}
          </div>
        </fieldset>
        <input
          aria-label="Webhook URL"
          placeholder="Revocation webhook (https, optional)"
          value={webhook}
          onChange={(e) => setWebhook(e.target.value)}
          className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm"
        />
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={firstParty} onChange={(e) => setFirstParty(e.target.checked)} />
          Operated by Dimagi
        </label>
        <button
          type="submit"
          disabled={busy !== null || !name.trim() || !operator.trim() || !redirects.trim() || scopes.size === 0}
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {busy === 'register' ? 'Registering…' : 'Register'}
        </button>
      </form>
    </div>
  )
}
