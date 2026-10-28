/**
 * Draws one page of a workspace app (apps/workspace_apps) — the dynamic-view
 * sandbox, with the app's calls wired in. The SAME frame, runtime, kit and
 * protocol as a dynamic view (`DynamicViewFrame`); what an app adds is the
 * `app` prop (query/act/history/upload/go, the app's fields and settings, the
 * workspace's slot fills), each call answered here through the app's routes —
 * as the viewer, as an anonymous visitor, or as a visitor previewed on a draft's
 * test copy — where the server applies the app's rules. Design:
 * docs/architecture/workspace-apps.md.
 */
import { useMemo, useState } from 'react'
import { callApp, type AppPage, type CollectionSource } from '@/api/workspaceApps'
import { getDynamicViewGuide, type DynamicViewSource } from '@/api/dynamicViews'
import { DynamicViewFrame, readSources } from '@/dynamicViews/DynamicViewFrame'
import type { AppContext, AppOp, ViewScope } from '@/dynamicViews/protocol'

export type Door = 'member' | 'preview' | 'public' | 'link'

interface Props {
  workspace: { slug: string; name: string }
  app: { key: string; name: string }
  page: AppPage
  roles: string[]
  params: Record<string, string>
  door: Door
  /** The draft whose test copy this page runs on, if any. */
  draft?: number | null
  /** On the link door: the private link's token, and the record it opens (the page's `data.record`). */
  link?: { token: string; record: Record<string, unknown> }
  dataModel: Record<string, unknown>
  settings: Record<string, unknown>
  viewer: { email: string; name: string }
  onGo: (page: string, params: Record<string, string>) => void
}

function isCollection(s: Record<string, unknown>): s is CollectionSource {
  return typeof s.collection === 'string'
}

export function AppFrame({ workspace, app, page, roles, params, door, draft, link, dataModel, settings, viewer, onGo }: Props) {
  const [error, setError] = useState<string | null>(null)
  const paramsKey = JSON.stringify(params)
  const rolesKey = roles.join(',')
  const modelKey = JSON.stringify([dataModel, settings, page.fills])

  const context: AppContext = useMemo(
    () => ({
      key: app.key, name: app.name, page: page.slug, params, roles, public: door !== 'member',
      dataModel, settings, fills: (page.fills ?? {}) as AppContext['fills'], draft: !!draft,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [app.key, app.name, page.slug, paramsKey, rolesKey, door, modelKey, draft],
  )

  const hosted = useMemo(() => {
    const call = (op: AppOp) => callApp(workspace.slug, app.key, op, door, draft, link?.token)
    return {
      context,
      call,
      go: onGo,
      canopyReads: door === 'member',
      async load() {
        if (door === 'link') return { record: link?.record ?? {} }
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
        if (canopy.length && door === 'member') {
          const guide = await getDynamicViewGuide()
          Object.assign(data, await readSources(guide.catalog, { workspace: workspace.slug, project: null }, canopy))
        }
        return data
      },
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [context, page, workspace.slug, door, draft, link?.token])

  const scope: ViewScope = useMemo(
    () => ({ workspace, project: null, view: { id: 0, slug: page.slug, title: page.title, version: 0 }, viewer }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [workspace.slug, page.slug, viewer.email],
  )
  const view = useMemo(() => ({ code: page.code, sources: [] }), [page.code])

  return (
    <>
      {error && <div className="mb-3 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}
      <DynamicViewFrame view={view} scope={scope} onError={setError} app={hosted} />
    </>
  )
}
