import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { getInstalledApp, type InstalledApp } from '@/api/workspaceApps'
import { useAuth } from '@/auth/AuthProvider'
import { useScopedPath } from '@/lib/scopedLinks'
import { AppFrame } from '@/workspaceApps/AppFrame'
import { useWorkspaceList } from '@/workspace/WorkspaceProvider'

/**
 * One installed app, for a member: its member pages as tabs (the ones the
 * viewer's app roles reach), the current page drawn in the sandbox. A page's
 * parameters are the query string, so every screen is a copyable link.
 *
 * `?draft=<id>` opens a draft's TEST COPY instead (maintainers): the draft's
 * version of the app over sample records, where nothing is sent and nothing
 * touches live data — and its public pages are drawn here too, as a visitor
 * would see them, so the whole change is tried before it is published.
 */
export default function WorkspaceAppPage() {
  const { workspace = '', appKey = '', page: pageSlug = '' } = useParams()
  const [search] = useSearchParams()
  const navigate = useNavigate()
  const scoped = useScopedPath()
  const { user } = useAuth()
  const workspaceName = useWorkspaceList().find((w) => w.slug === workspace)?.display_name ?? workspace
  const draft = Number(search.get('draft')) || null
  const [app, setApp] = useState<InstalledApp | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setApp(null)
    getInstalledApp(workspace, appKey, draft).then(
      (a) => !cancelled && setApp(a),
      (e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)),
    )
    return () => {
      cancelled = true
    }
  }, [workspace, appKey, draft])

  if (error) return <div className="p-6 text-sm text-destructive">Couldn’t load the app: {error}</div>
  if (!app) return <div className="p-6 text-sm text-muted-foreground">Loading…</div>

  const memberPages = app.pages.filter((p) => p.audience === 'member')
  const publicPages = app.pages.filter((p) => p.audience === 'public')
  // In a test copy, public pages are tabs too (previewed as a visitor).
  const tabs = draft ? [...memberPages, ...publicPages] : memberPages
  const page = tabs.find((p) => p.slug === pageSlug) ?? tabs.find((p) => p.nav) ?? tabs[0]
  const params = Object.fromEntries([...search.entries()].filter(([k]) => k !== 'draft'))
  const go = (slug: string, p: Record<string, string>) => {
    const qs = new URLSearchParams({ ...p, ...(draft ? { draft: String(draft) } : {}) }).toString()
    navigate(scoped(`/apps/${app.key}/${slug}${qs ? `?${qs}` : ''}`))
  }
  const tabHref = (slug: string) => scoped(`/apps/${app.key}/${slug}${draft ? `?draft=${draft}` : ''}`)

  return (
    <div className="mx-auto max-w-7xl p-6">
      {draft && (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-lg border-2 border-warning bg-warning/10 px-4 py-2 text-sm">
          <div>
            <span className="font-semibold">TEST COPY</span> — draft v{app.draft?.number}
            {app.draft?.origin === 'upgrade' ? ` (template ${app.draft?.template_version})` : ''}. Sample data only; nothing you do
            here reaches the live app or emails anyone.
          </div>
          <div className="flex gap-3">
            <Link className="font-medium underline" to={scoped(`/apps/${app.key}/customize`)}>Back to the draft</Link>
            <Link className="underline" to={scoped(`/apps/${app.key}`)}>Leave the test copy</Link>
          </div>
        </div>
      )}
      <header className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="text-xs text-muted-foreground"><Link className="hover:underline" to={scoped('/apps')}>Apps</Link></div>
          <h1 className="text-2xl font-semibold">{app.name}</h1>
        </div>
        <nav className="flex flex-wrap items-center gap-1 text-sm">
          {tabs.filter((p) => p.nav).map((p) => (
            <Link key={p.slug} to={tabHref(p.slug)}
              className={`rounded-md px-3 py-1.5 ${p.slug === page?.slug ? 'bg-muted font-medium' : 'text-muted-foreground hover:bg-muted'}`}>
              {p.title}{p.audience === 'public' ? ' (public)' : ''}
            </Link>
          ))}
          {!draft && publicPages.length > 0 && (
            <a className="rounded-md px-3 py-1.5 text-muted-foreground hover:bg-muted" target="_blank" rel="noreferrer"
              href={`${import.meta.env.BASE_URL.replace(/\/$/, '')}${scoped(`/app/${app.key}`)}`}>
              Public pages{app.public ? '' : ' (not live)'} ↗
            </a>
          )}
          {app.may_maintain && !draft && (
            <Link to={scoped(`/apps/${app.key}/customize`)}
              className="ml-2 rounded-md border border-border px-3 py-1.5 font-medium hover:bg-muted">
              Customize{app.has_draft ? ' (draft open)' : app.upgrade_available ? ' (update available)' : ''}
            </Link>
          )}
        </nav>
      </header>
      {!app.enabled && (
        <div className="mb-3 rounded-md border border-warning/40 bg-warning/10 p-3 text-sm">This app is switched off: only those who manage apps can open it.</div>
      )}
      {page ? (
        <AppFrame
          key={`${draft}:${page.slug}?${search.toString()}`}
          workspace={{ slug: workspace, name: workspaceName }}
          app={{ key: app.key, name: app.name }}
          page={page}
          roles={page.audience === 'public' ? ['public'] : [...app.my_roles]}
          params={params}
          door={page.audience === 'public' ? 'preview' : 'member'}
          draft={draft}
          dataModel={app.data_model as Record<string, unknown>}
          settings={app.settings as Record<string, unknown>}
          viewer={{ email: user?.email ?? '', name: (user as { display_name?: string } | null)?.display_name ?? '' }}
          onGo={go}
        />
      ) : (
        <div className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
          This app has no page for your role here. Ask a workspace admin for one of its roles.
        </div>
      )}
    </div>
  )
}
