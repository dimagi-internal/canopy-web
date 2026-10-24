import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { createDynamicView, getDynamicViewGuide, listDynamicViews, type DynamicViewSummary } from '@/api/dynamicViews'
import { useScopedPath } from '@/lib/scopedLinks'
import { useWorkspaceList } from '@/workspace/WorkspaceProvider'
import type { DynamicViewSource } from '@/api/dynamicViews'

/**
 * A workspace's dynamic views: AI-generated screens over canopy's own data, each
 * drawn in a sandbox (docs/architecture/dynamic-views.md). One row per view, the
 * workspace's own first, then each project's. `?project=<handle>` narrows to one
 * project, and `?new=1` opens the "new view" form (the project page links here).
 */

export const EDIT_ROLES = ['editor', 'admin', 'owner']

export function useCanEditViews(workspace: string): boolean {
  const role = useWorkspaceList().find((w) => w.slug === workspace)?.role ?? ''
  return EDIT_ROLES.includes(role)
}

export default function DynamicViewsPage() {
  const { workspace = '' } = useParams()
  const [search] = useSearchParams()
  const project = search.get('project') || ''
  const scoped = useScopedPath()
  const canEdit = useCanEditViews(workspace)
  const [rows, setRows] = useState<DynamicViewSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(search.get('new') === '1')

  useEffect(() => {
    let cancelled = false
    setRows(null)
    listDynamicViews(project)
      .then((r) => !cancelled && setRows(r))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      cancelled = true
    }
  }, [project, workspace])

  return (
    <div className="mx-auto max-w-5xl p-6">
      <header className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h1 className="text-2xl font-semibold">Views</h1>
          <p className="text-sm text-muted-foreground">
            Screens generated over this workspace’s tasks, projects, agents and people — each runs in a sandbox and
            shows only what you can already read.
            {project && (
              <>
                {' '}Showing project <span className="font-mono">{project}</span> ·{' '}
                <Link className="underline" to={scoped('/views')}>all views</Link>
              </>
            )}
          </p>
        </div>
        {canEdit && !creating && (
          <button type="button" className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" onClick={() => setCreating(true)}>
            New view
          </button>
        )}
      </header>

      {creating && canEdit && <NewViewForm workspace={workspace} project={project} onCancel={() => setCreating(false)} />}

      <p className="mb-4 text-sm text-muted-foreground">
        To generate one, ask an agent — for example <em>“make a view of this project’s open tasks by assignee”</em>.
        It reads the contract with the <code>get_dynamic_view_guide</code> tool and saves the view with{' '}
        <code>create_dynamic_view</code>.
      </p>

      {error && <div className="text-sm text-destructive">Couldn’t load views: {error}</div>}
      {!error && rows === null && <div className="text-sm text-muted-foreground">Loading…</div>}
      {rows && rows.length === 0 && <div className="text-sm text-muted-foreground">No views yet.</div>}
      {rows && rows.length > 0 && (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="px-2 py-1.5 font-medium">View</th>
              <th className="px-2 py-1.5 font-medium">For</th>
              <th className="px-2 py-1.5 text-right font-medium">Version</th>
              <th className="px-2 py-1.5 font-medium">Last changed</th>
            </tr>
          </thead>
          <tbody>
            {[...rows]
              .sort((a, b) => Number(!!a.project) - Number(!!b.project) || a.title.localeCompare(b.title))
              .map((v) => (
                <tr key={v.id} className="border-b border-border last:border-0">
                  <td className="px-2 py-1.5">
                    <Link className="font-medium hover:underline" to={scoped(`/views/${v.id}`)}>{v.title}</Link>
                    {v.description && <div className="text-xs text-muted-foreground">{v.description}</div>}
                  </td>
                  <td className="px-2 py-1.5">
                    {v.project ? (
                      <Link className="hover:underline" to={scoped(`/projects/${v.project}`)}>{v.project_name ?? v.project}</Link>
                    ) : (
                      <span className="text-muted-foreground">Workspace</span>
                    )}
                  </td>
                  <td className="px-2 py-1.5 text-right tabular-nums">{v.version}</td>
                  <td className="px-2 py-1.5 text-muted-foreground">
                    {new Date(v.updated_at).toLocaleString()}
                    {v.updated_by_email ? ` · ${v.updated_by_email}` : ''}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function NewViewForm({ workspace, project, onCancel }: { workspace: string; project: string; onCancel: () => void }) {
  const navigate = useNavigate()
  const scoped = useScopedPath()
  const [title, setTitle] = useState('')
  const [forProject, setForProject] = useState(project)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const guide = await getDynamicViewGuide()
      const view = await createDynamicView({
        workspace,
        project: forProject.trim() || null,
        title: title.trim() || 'New view',
        code: guide.example,
        sources: guide.example_sources as DynamicViewSource[],
      })
      navigate(scoped(`/views/${view.id}?edit=1`))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="mb-4 flex flex-wrap items-end gap-3 rounded-lg border border-border bg-card p-4">
      <label className="flex flex-col gap-1 text-sm">
        Title
        <input className="rounded-md border border-input bg-background px-2 py-1" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Open work by assignee" autoFocus />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        Project handle (blank for the workspace)
        <input className="rounded-md border border-input bg-background px-2 py-1 font-mono" value={forProject} onChange={(e) => setForProject(e.target.value)} placeholder="unga-2026" />
      </label>
      <button type="submit" disabled={busy} className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50">
        {busy ? 'Creating…' : 'Create from the example'}
      </button>
      <button type="button" className="text-sm text-muted-foreground hover:underline" onClick={onCancel}>Cancel</button>
      {error && <div className="basis-full text-sm text-destructive">{error}</div>}
    </form>
  )
}
