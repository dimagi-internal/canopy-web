import { useEffect, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { appStoreScreenshotUrl, getAppStoreDetail, installApp, type StoreAppDetail } from '@/api/workspaceApps'
import {
  AppAsideSection,
  AppDetailLayout,
  AppDetailPending,
  AppDetailSection,
  AppOverview,
  AppVersions,
} from '@/components/apps/AppDetailLayout'
import { roleAllows } from '@/lib/workspaceRoles'
import { useScopedPath } from '@/lib/scopedLinks'
import { useWorkspaceList } from '@/workspace/WorkspaceProvider'

/**
 * One app in the store, as someone deciding whether to install it reads it:
 * screenshots, what it does and for whom, the roles it adds and who holds them,
 * the records it keeps (and which only one role reads), what it shows the
 * public, the emails it sends, and its versions. Admins install from here.
 */
export default function WorkspaceAppStoreDetailPage() {
  const { workspace = '' } = useParams()
  const [search] = useSearchParams()
  const template = search.get('template') ?? ''
  const scoped = useScopedPath()
  const navigate = useNavigate()
  const role = useWorkspaceList().find((w) => w.slug === workspace)?.role ?? ''
  const manage = roleAllows(role, 'integrations')
  const [app, setApp] = useState<StoreAppDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [shot, setShot] = useState(0)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setApp(null)
    setError(null)
    getAppStoreDetail(workspace, template).then(setApp, (e: unknown) => setError(e instanceof Error ? e.message : String(e)))
  }, [workspace, template])

  async function install() {
    if (!app) return
    setBusy(true)
    setError(null)
    try {
      const inst = await installApp(workspace, app.key)
      navigate(scoped(`/apps/${inst.key}`))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setBusy(false)
    }
  }

  if (!app) return <AppDetailPending back={scoped('/apps')} error={error} />

  const shots = app.listing.screenshots
  const current = shots[Math.min(shot, shots.length - 1)]
  const src = (file: string) => appStoreScreenshotUrl(workspace, app.key, file)
  const memberPages = app.pages.filter((p) => p.audience === 'member')
  const otherPages = app.pages.filter((p) => p.audience !== 'member')

  return (
    <AppDetailLayout
      back={scoped('/apps')}
      name={app.name}
      byline={`by ${app.publisher} · version ${app.version}`}
      tagline={app.listing.tagline}
      error={error}
      action={
        <>
          {app.installed ? (
            <span className="rounded-md bg-muted px-3 py-1.5 text-muted-foreground">Installed in this workspace</span>
          ) : manage ? (
            <button type="button" disabled={busy} onClick={install}
              className="rounded-md bg-primary px-4 py-2 font-medium text-primary-foreground hover:opacity-75 disabled:opacity-50">
              {busy ? 'Installing…' : 'Install'}
            </button>
          ) : (
            <span className="text-muted-foreground">A workspace admin can install it.</span>
          )}
          {!app.installed && <span className="max-w-xs text-right text-xs text-muted-foreground">Installs switched on, with its public pages off.</span>}
        </>
      }
      media={current && (
        <section aria-label="Screenshots" className="mt-6">
          <a href={src(current.file)} target="_blank" rel="noreferrer" className="block overflow-hidden rounded-lg border border-border bg-muted">
            <img src={src(current.file)} alt={current.caption || app.name} className="block w-full" />
          </a>
          {current.caption && <p className="mt-2 text-sm text-muted-foreground">{current.caption}</p>}
          {shots.length > 1 && (
            <div className="mt-3 flex gap-2 overflow-x-auto pb-1">
              {shots.map((s, i) => (
                <button key={s.file} type="button" onClick={() => setShot(i)} aria-label={`Screenshot ${i + 1}: ${s.caption}`}
                  aria-current={i === shot}
                  className={`shrink-0 overflow-hidden rounded-md border-2 ${i === shot ? 'border-primary' : 'border-transparent opacity-70 hover:opacity-100'}`}>
                  <img src={src(s.file)} alt="" className="block h-16 w-28 object-cover object-top" loading="lazy" />
                </button>
              ))}
            </div>
          )}
        </section>
      )}
      aside={
        <>
          <AppAsideSection title="Pages">
            <ul className="mt-1 text-muted-foreground">{memberPages.map((p) => <li key={p.title}>{p.title}</li>)}</ul>
          </AppAsideSection>
          {otherPages.length > 0 && (
            <AppAsideSection title="Outside the workspace">
              <ul className="mt-1 text-muted-foreground">
                {otherPages.map((p) => <li key={p.title}>{p.title} <span className="text-xs">({p.audience === 'public' ? 'public' : 'private link'})</span></li>)}
              </ul>
              {app.public_pages.length > 0 && <p className="mt-1 text-xs text-muted-foreground">Public pages stay off until an admin turns them on.</p>}
            </AppAsideSection>
          )}
          {app.emails.length > 0 && (
            <AppAsideSection title="Emails it can send">
              <ul className="mt-1 space-y-1 text-muted-foreground">
                {app.emails.map((e) => (
                  <li key={e.name}>
                    <span className="text-foreground">{e.name}</span>
                    {e.to && <span className="text-xs"> · {e.to}</span>}
                    {(e.optional || e.delayable) && (
                      <span className="block text-xs">{[e.optional && 'can be switched off', e.delayable && 'can be held back'].filter(Boolean).join(', ')}</span>
                    )}
                  </li>
                ))}
              </ul>
              <p className="mt-1 text-xs text-muted-foreground">Worded by settings you can change. Sent only once the server's app email is switched on.</p>
            </AppAsideSection>
          )}
          <AppAsideSection title="Customizable">
            <p className="mt-1 text-muted-foreground">{app.settings} settings, plus your own fields, stages and pages. Template updates keep arriving as a draft to test.</p>
          </AppAsideSection>
          <AppVersions current={app.version} versions={app.versions} />
        </>
      }
    >
      <AppOverview text={app.listing.overview || app.description} />

      {app.listing.highlights.length > 0 && (
        <AppDetailSection title="Highlights">
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            {app.listing.highlights.map((h) => (
              <div key={h.title} className="rounded-lg border border-border bg-card p-3">
                <div className="text-sm font-medium">{h.title}</div>
                <p className="mt-1 text-sm text-muted-foreground">{h.body}</p>
              </div>
            ))}
          </div>
        </AppDetailSection>
      )}

      <AppDetailSection title="Who uses it">
        {app.listing.audience && <p className="mt-2 max-w-prose text-sm">{app.listing.audience}</p>}
        <div className="mt-3 divide-y divide-border rounded-lg border border-border bg-card">
          {app.roles.map((r) => (
            <div key={r.name} className="p-3 text-sm">
              <div><span className="font-medium capitalize">{r.name}</span> <span className="text-xs text-muted-foreground">— {r.holders}</span></div>
              {r.description && <p className="mt-0.5 text-muted-foreground">{r.description}</p>}
            </div>
          ))}
        </div>
      </AppDetailSection>

      <AppDetailSection title="What it keeps">
        {app.listing.data && <p className="mt-2 max-w-prose text-sm">{app.listing.data}</p>}
        <ul className="mt-3 flex flex-wrap gap-2 text-xs">
          {app.records.map((r) => (
            <li key={r.label} className="rounded-md border border-border bg-card px-2 py-1">
              {r.label}
              {r.scoped && <span className="ml-1 text-muted-foreground">· per-job access</span>}
              {r.restricted.length > 0 && <span className="ml-1 text-warning">· some fields {r.restricted.join(', ')} only</span>}
            </li>
          ))}
        </ul>
        {app.retention.length > 0 && (
          <>
            <h3 className="mt-4 text-sm font-medium">Kept no longer than needed</h3>
            <ul className="mt-1 list-disc pl-5 text-sm text-muted-foreground">
              {app.retention.map((r) => <li key={r}>{r}</li>)}
            </ul>
          </>
        )}
      </AppDetailSection>
    </AppDetailLayout>
  )
}
