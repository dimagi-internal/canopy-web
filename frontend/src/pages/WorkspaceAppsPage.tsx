import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  installApp,
  listAppStore,
  listInstalledApps,
  uninstallApp,
  updateInstalledApp,
  type InstalledAppSummary,
  type StoreApp,
} from '@/api/workspaceApps'
import { roleAllows } from '@/lib/workspaceRoles'
import { useScopedPath } from '@/lib/scopedLinks'
import { useWorkspaceList } from '@/workspace/WorkspaceProvider'

/**
 * The workspace's apps, and the store they come from. An installed app is the
 * workspace's own: it customizes it (Customize) and still receives the
 * template's updates, which arrive as a draft to test. Admins install, switch
 * apps on or off, make public pages live, or remove an app with its data.
 * Design: docs/architecture/workspace-apps.md.
 */
export default function WorkspaceAppsPage() {
  const { workspace = '' } = useParams()
  const scoped = useScopedPath()
  const role = useWorkspaceList().find((w) => w.slug === workspace)?.role ?? ''
  const manage = roleAllows(role, 'integrations')
  const [installed, setInstalled] = useState<InstalledAppSummary[] | null>(null)
  const [store, setStore] = useState<StoreApp[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const load = useCallback(() => {
    listInstalledApps(workspace).then(setInstalled, (e: unknown) => setError(e instanceof Error ? e.message : String(e)))
    listAppStore(workspace).then(setStore, () => setStore([]))
  }, [workspace])
  useEffect(load, [load])

  async function run(key: string, fn: () => Promise<unknown>) {
    setBusy(key)
    setError(null)
    try {
      await fn()
      load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const have = new Set((installed ?? []).map((a) => a.template))
  const publicUrl = (key: string) => `${window.location.origin}${import.meta.env.BASE_URL.replace(/\/$/, '')}${scoped(`/app/${key}`)}`

  return (
    <div className="mx-auto max-w-5xl p-6">
      <h1 className="text-2xl font-semibold">Apps</h1>
      <p className="mt-1 max-w-prose text-sm text-muted-foreground">
        Apps this workspace runs. Each keeps its own records, uses the workspace's roles, and can be customized —
        new fields, stages, wording, whole pages — without losing the updates its template ships.
      </p>
      {error && <div className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}

      <h2 className="mb-2 mt-8 text-sm font-semibold uppercase tracking-wide text-muted-foreground">Installed</h2>
      {installed === null ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : installed.length === 0 ? (
        <div className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">No apps yet.{manage ? ' Install one from the store below.' : ''}</div>
      ) : (
        <div className="divide-y divide-border rounded-lg border border-border bg-card">
          {installed.map((a) => (
            <div key={a.key} className="flex flex-wrap items-start justify-between gap-3 p-4">
              <div className="min-w-0 max-w-prose">
                <Link className="font-medium hover:underline" to={scoped(`/apps/${a.key}`)}>{a.name}</Link>
                <span className="ml-2 text-xs text-muted-foreground">version {a.live_version} · template {a.template} {a.template_version}</span>
                {!a.enabled && <span className="ml-2 rounded bg-muted px-1.5 py-0.5 text-xs">off</span>}
                {a.has_draft && <span className="ml-2 rounded bg-warning/15 px-1.5 py-0.5 text-xs">draft open</span>}
                {a.upgrade_available && (
                  <Link to={scoped(`/apps/${a.key}/customize`)} className="ml-2 rounded bg-primary/15 px-1.5 py-0.5 text-xs text-primary">
                    update {a.upgrade_available} available
                  </Link>
                )}
                <p className="mt-1 text-sm text-muted-foreground">{a.description}</p>
                {a.public && a.enabled && a.has_public_pages && (
                  <p className="mt-1 text-xs">Public at <a className="text-primary hover:underline" href={publicUrl(a.key)} target="_blank" rel="noreferrer">{publicUrl(a.key)}</a></p>
                )}
              </div>
              {manage && (
                <div className="flex flex-wrap items-center gap-3 text-sm">
                  <label className="flex items-center gap-1.5">
                    <input type="checkbox" checked={a.enabled} disabled={busy === a.key}
                      onChange={(e) => run(a.key, () => updateInstalledApp(workspace, a.key, { enabled: e.target.checked }))} />
                    On
                  </label>
                  {a.has_public_pages && (
                    <label className="flex items-center gap-1.5">
                      <input type="checkbox" checked={a.public} disabled={busy === a.key}
                        onChange={(e) => run(a.key, () => updateInstalledApp(workspace, a.key, { public: e.target.checked }))} />
                      Public pages
                    </label>
                  )}
                  <button type="button" className="text-destructive hover:underline" disabled={busy === a.key}
                    onClick={() => window.confirm(`Remove ${a.name} and every record it holds? This cannot be undone.`)
                      && run(a.key, () => uninstallApp(workspace, a.key))}>
                    Remove
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <h2 className="mb-2 mt-8 text-sm font-semibold uppercase tracking-wide text-muted-foreground">App store</h2>
      <div className="grid gap-3 sm:grid-cols-2">
        {store.map((a) => (
          <div key={a.key} className="flex flex-col justify-between gap-3 rounded-lg border border-border bg-card p-4">
            <div>
              <div className="font-medium">{a.name}</div>
              <div className="text-xs text-muted-foreground">by {a.publisher} · {a.version}</div>
              <p className="mt-2 text-sm text-muted-foreground">{a.description}</p>
            </div>
            <div>
              {have.has(a.key) ? (
                <span className="text-sm text-muted-foreground">Installed</span>
              ) : manage ? (
                <button type="button" className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-75"
                  disabled={busy === a.key} onClick={() => run(a.key, () => installApp(workspace, a.key))}>
                  Install
                </button>
              ) : (
                <span className="text-sm text-muted-foreground">An admin can install it.</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
