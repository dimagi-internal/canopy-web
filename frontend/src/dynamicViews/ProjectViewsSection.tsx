/**
 * A project's dynamic views, drawn on its page — each in its own sandbox. Hidden
 * entirely while the project has none and the viewer cannot make one, so a
 * project page carries no empty section by default.
 */
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getDynamicView, listDynamicViews, type DynamicView } from '@/api/dynamicViews'
import { useAuth } from '@/auth/AuthProvider'
import { Section } from '@/components/objectPage/settingsLayout'
import { scopedPath } from '@/lib/scopedLinks'
import { useCanEditViews } from '@/pages/DynamicViewsPage'
import { useWorkspaceList } from '@/workspace/WorkspaceProvider'
import { DynamicViewFrame } from './DynamicViewFrame'
import type { DynamicViewSource } from '@/api/dynamicViews'

/** How many of a project's views draw on its page; the rest are links. */
const DRAWN = 3

export function ProjectViewsSection({ workspace, handle, projectName }: { workspace: string; handle: string; projectName: string }) {
  const canEdit = useCanEditViews(workspace)
  const workspaceName = useWorkspaceList().find((w) => w.slug === workspace)?.display_name ?? workspace
  const { user } = useAuth()
  const [views, setViews] = useState<DynamicView[] | null>(null)
  const [more, setMore] = useState<{ id: number; title: string }[]>([])

  useEffect(() => {
    let cancelled = false
    listDynamicViews(handle)
      .then(async (rows) => {
        const own = rows.filter((r) => r.workspace === workspace && r.project === handle)
        const drawn = await Promise.all(own.slice(0, DRAWN).map((r) => getDynamicView(r.id)))
        if (cancelled) return
        setViews(drawn)
        setMore(own.slice(DRAWN).map((r) => ({ id: r.id, title: r.title })))
      })
      .catch(() => !cancelled && setViews([]))
    return () => {
      cancelled = true
    }
  }, [workspace, handle])

  if (views === null || (views.length === 0 && !canEdit)) return null
  const newHref = scopedPath(workspace, `/views?project=${encodeURIComponent(handle)}&new=1`)
  return (
    <Section
      id="project-views"
      title="Views"
      description="Screens generated over this project's data. Each runs in a sandbox and shows only what you can already read."
    >
      {views.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">
          No views yet. Ask an agent to make one for this project, or{' '}
          <Link className="underline" to={newHref}>start one from the example</Link>.
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          {views.map((v) => (
            <div key={v.id} className="min-w-0">
              <div className="mb-1 flex items-baseline justify-between gap-2">
                <Link className="text-[13px] font-semibold hover:underline" to={scopedPath(workspace, `/views/${v.id}`)}>
                  {v.title}
                </Link>
                <span className="text-[11.5px] text-muted-foreground">v{v.version}</span>
              </div>
              <DynamicViewFrame
                view={{ code: v.code, sources: v.sources as DynamicViewSource[] }}
                scope={{
                  workspace: { slug: workspace, name: workspaceName },
                  project: { handle, name: projectName },
                  view: { id: v.id, slug: v.slug, title: v.title, version: v.version },
                  viewer: { email: user?.email ?? '', name: (user as { display_name?: string } | null)?.display_name ?? '' },
                }}
              />
            </div>
          ))}
          {(more.length > 0 || canEdit) && (
            <div className="flex flex-wrap gap-3 text-[13px]">
              {more.map((m) => (
                <Link key={m.id} className="underline" to={scopedPath(workspace, `/views/${m.id}`)}>{m.title}</Link>
              ))}
              {canEdit && <Link className="text-muted-foreground underline" to={newHref}>New view</Link>}
            </div>
          )}
        </div>
      )}
    </Section>
  )
}
