/**
 * Draws one page of a workspace app (apps/workspace_apps) — the dynamic-view
 * sandbox, with the app's calls wired in. The SAME frame, runtime, kit and
 * protocol as a dynamic view (`DynamicViewFrame`); what an app adds is the
 * `app` prop (query/act/history/upload/go, the app's fields and settings, the
 * workspace's slot fills), each call answered here through the app's routes —
 * as the viewer, as an anonymous visitor, or as a visitor previewed on a draft's
 * test copy — where the server applies the app's rules. A bundle page
 * (`kind: "bundle"`) is the same frame loading one of the app's own HTML files
 * instead of compiling code; its page params ride in the file's query string too,
 * for code that reads `location.search`. Design: docs/architecture/workspace-apps.md.
 */
import { useMemo, useRef, useState } from 'react'
import { callApp, type AppPage, type CollectionSource } from '@/api/workspaceApps'
import { getDynamicViewGuide, type DynamicViewSource } from '@/api/dynamicViews'
import { DynamicViewFrame, readSources, type BrowserHost } from '@/dynamicViews/DynamicViewFrame'
import type { DeclaredConnection } from '@/dynamicViews/connectionCalls'
import type { AppContext, AppOp, ViewScope } from '@/dynamicViews/protocol'

export type Surface = 'member' | 'preview' | 'public' | 'link'

interface Props {
  workspace: { slug: string; name: string }
  app: { key: string; name: string }
  page: AppPage
  roles: string[]
  params: Record<string, string>
  surface: Surface
  /** The draft whose test copy this page runs on, if any. */
  draft?: number | null
  /** On the link surface: the private link's token, and the record it opens (the page's `data.record`). */
  link?: { token: string; record: Record<string, unknown> }
  dataModel: Record<string, unknown>
  settings: Record<string, unknown>
  /** The app's declared connections (member surface); the server stays the authority. */
  connections?: readonly DeclaredConnection[]
  viewer: { email: string; name: string }
  onGo: (page: string, params: Record<string, string>) => void
  /** `canopy.page.setParams`: write the page's params into the host's own URL, with no reload and no new frame. */
  onParams?: (params: Record<string, string>) => void
  /** A full-bleed page (`layout: "full"`): the frame fills the box its host gives it. */
  fill?: boolean
  /** The canopy URL of a page of the app, so a bundle page can open one in a new tab. */
  pageHref?: (page: string, params: Record<string, string>) => string
  /** The app's pages, so a bundle page can open another by its entry file. */
  pages?: readonly AppPage[]
  /** Drawn in the browser extension's panel (member surface only): what `canopy.browser` reaches. */
  browser?: BrowserHost
}

function isCollection(s: Record<string, unknown>): s is CollectionSource {
  return typeof s.collection === 'string'
}

export function AppFrame({ workspace, app, page, roles, params, surface, draft, link, dataModel, settings, connections, viewer, onGo, onParams, fill, pageHref, pages, browser }: Props) {
  const [error, setError] = useState<string | null>(null)
  // Read through a ref, so a new function from the host each render keeps the frame's `app` the same.
  const paramsTo = useRef(onParams)
  paramsTo.current = onParams
  const paramsKey = JSON.stringify(params)
  const rolesKey = roles.join(',')
  const modelKey = JSON.stringify([dataModel, settings, page.fills])

  const context: AppContext = useMemo(
    () => ({
      key: app.key, name: app.name, page: page.slug, params, roles, public: surface !== 'member',
      dataModel, settings, fills: (page.fills ?? {}) as AppContext['fills'], draft: !!draft,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [app.key, app.name, page.slug, paramsKey, rolesKey, surface, modelKey, draft],
  )

  const hosted = useMemo(() => {
    const call = (op: AppOp) => callApp(workspace.slug, app.key, op, surface, draft, link?.token)
    return {
      context,
      call,
      go: onGo,
      setParams: onParams ? (p: Record<string, string>) => paramsTo.current?.(p) : undefined,
      href: pageHref,
      canopyReads: surface === 'member',
      canConnect: surface === 'member',
      connections: surface === 'member' ? connections : undefined,
      async load() {
        if (surface === 'link') return { record: link?.record ?? {} }
        const sources = page.sources as Record<string, unknown>[]
        const own = sources.filter(isCollection)
        const canopy = sources.filter((s) => !isCollection(s)) as unknown as DynamicViewSource[]
        const entries = await Promise.all(own.map(async (s) => {
          try {
            return [s.alias, await call({ op: 'query', collection: s.collection, where: s.where, order: s.order, limit: s.limit })] as const
          } catch (e) {
            return [s.alias, { error: e instanceof Error ? e.message : String(e) }] as const
          }
        }))
        const data: Record<string, unknown> = Object.fromEntries(entries)
        if (canopy.length && surface === 'member') {
          const guide = await getDynamicViewGuide()
          Object.assign(data, await readSources(guide.catalog, { workspace: workspace.slug, project: null }, canopy))
        }
        return data
      },
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [context, page, workspace.slug, surface, draft, link?.token, connections, !!onParams])

  const scope: ViewScope = useMemo(
    () => ({ workspace, project: null, view: { id: 0, slug: page.slug, title: page.title, version: 0 }, viewer }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [workspace.slug, page.slug, viewer.email],
  )
  const bundlePages = (pages ?? [page]).filter((p) => p.kind === 'bundle').map((p) => ({ slug: p.slug, entry: p.entry }))
  const bundleKey = JSON.stringify(bundlePages)
  const view = useMemo(() => {
    if (page.kind !== 'bundle' || !page.bundle_url) return { code: page.code, sources: [] }
    const qs = new URLSearchParams(params).toString()
    return { code: '', sources: [], bundle: { src: `${page.bundle_url}${qs ? `?${qs}` : ''}`, pages: bundlePages } }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page.code, page.kind, page.bundle_url, paramsKey, bundleKey])

  return (
    <>
      {error && <div className={`${fill ? 'absolute inset-x-0 top-0 z-10 m-2' : 'mb-3'} rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive`}>{error}</div>}
      {/* A live member page is the page canopy's panel is on: it may describe itself to the agent bound
          to this app and offer it actions (its `page_*` tools), as a view does on its own page. */}
      <DynamicViewFrame view={view} scope={scope} onError={setError} app={hosted} pagePanel={surface === 'member' && !draft}
        browser={surface === 'member' && !draft ? browser : undefined} fill={fill} />
    </>
  )
}
