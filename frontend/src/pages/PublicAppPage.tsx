import { useEffect, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { getPublicApp, getPublicFeed, type PublicApp } from '@/api/workspaceApps'
import { scopedPath } from '@/lib/scopedLinks'
import { AppFrame } from '@/workspaceApps/AppFrame'

/**
 * A workspace app's PUBLIC pages, for anyone — no account, no canopy chrome
 * (`/w/:workspace/app/:appKey/:page?`, a scoped viewer in the server's
 * allowlist). The page reads only the app's public projections and submits only
 * its public, create-only actions; the server 404s all of it unless the app is
 * both switched on and made public.
 */
export default function PublicAppPage() {
  const { workspace = '', appKey = '', page: pageSlug = '' } = useParams()
  const [search] = useSearchParams()
  const navigate = useNavigate()
  const [app, setApp] = useState<PublicApp | null>(null)
  const [missing, setMissing] = useState(false)

  useEffect(() => {
    let cancelled = false
    getPublicApp(workspace, appKey).then(
      (a) => {
        if (cancelled) return
        setApp(a)
        document.title = `${a.name} · ${a.workspace_name}`
      },
      () => !cancelled && setMissing(true),
    )
    return () => {
      cancelled = true
    }
  }, [workspace, appKey])

  // Google for Jobs reads JobPosting structured data from the page itself: put
  // the entry this page shows (?id=) in the document head, from the app's feed.
  const pageSlugNow = app ? ((app.pages.find((p) => p.slug === pageSlug) ?? app.pages[0]).slug) : ''
  const shownId = search.get('id') ?? ''
  useEffect(() => {
    const feeds = ((app?.data_model as Record<string, unknown> | undefined)?._feeds ?? {}) as Record<string, string>
    const collection = Object.keys(feeds).find((c) => feeds[c] === pageSlugNow)
    if (!app || !collection || !shownId) return
    let tag: HTMLScriptElement | null = null
    getPublicFeed(workspace, appKey, collection).then((entries) => {
      const entry = entries.find((e) => String((e.identifier as { value?: string } | undefined)?.value) === shownId)
      if (!entry) return
      tag = document.createElement('script')
      tag.type = 'application/ld+json'
      tag.textContent = JSON.stringify(entry)
      document.head.appendChild(tag)
    }, () => {})
    return () => {
      tag?.remove()
    }
  }, [app, workspace, appKey, pageSlugNow, shownId])

  if (missing) return <div className="mx-auto max-w-xl p-10 text-center text-muted-foreground">This page is not available.</div>
  if (!app) return <div className="p-10 text-center text-sm text-muted-foreground">Loading…</div>

  const page = app.pages.find((p) => p.slug === pageSlug) ?? app.pages.find((p) => p.nav) ?? app.pages[0]
  const go = (slug: string, p: Record<string, string>) => {
    const qs = new URLSearchParams(p).toString()
    navigate(scopedPath(workspace, `/app/${app.key}/${slug}${qs ? `?${qs}` : ''}`))
    window.scrollTo(0, 0)
  }

  return (
    <div className="min-h-screen bg-background">
      <AppFrame
        key={`${page.slug}?${search.toString()}`}
        workspace={{ slug: app.workspace, name: app.workspace_name }}
        app={{ key: app.key, name: app.name }}
        page={page}
        roles={['public']}
        params={Object.fromEntries(search.entries())}
        door="public"
        dataModel={app.data_model as Record<string, unknown>}
        settings={app.settings as Record<string, unknown>}
        viewer={{ email: '', name: '' }}
        onGo={go}
      />
    </div>
  )
}
