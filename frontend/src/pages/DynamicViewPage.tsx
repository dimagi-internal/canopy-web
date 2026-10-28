import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  deleteDynamicView,
  getDynamicView,
  listDynamicViewRevisions,
  rollbackDynamicView,
  updateDynamicView,
  type DynamicView,
  type DynamicViewRevision,
  type DynamicViewSource,
} from '@/api/dynamicViews'
import { useAuth } from '@/auth/AuthProvider'
import type { DeclaredTask } from '@/dynamicViews/agentTasks'
import { DeclaredTasks } from '@/dynamicViews/DeclaredTasks'
import { DynamicViewFrame, type FrameView } from '@/dynamicViews/DynamicViewFrame'
import type { ViewScope } from '@/dynamicViews/protocol'
import { useScopedPath } from '@/lib/scopedLinks'
import { useWorkspaceList } from '@/workspace/WorkspaceProvider'
import { useCanEditViews } from './DynamicViewsPage'

/**
 * One dynamic view, drawn in its sandbox (`DynamicViewFrame`). Everyone sees the
 * agent tasks it may start (its one way to make something happen). Editors get the
 * code, its sources and its agent tasks beside a live preview — the preview draws
 * the DRAFT, so nothing is saved until Save — and the version history with Restore.
 */
export default function DynamicViewPage() {
  const { workspace = '', id = '' } = useParams()
  const [search, setSearch] = useSearchParams()
  const scoped = useScopedPath()
  const navigate = useNavigate()
  const canEdit = useCanEditViews(workspace)
  const workspaceName = useWorkspaceList().find((w) => w.slug === workspace)?.display_name ?? workspace
  const { user } = useAuth()
  const [view, setView] = useState<DynamicView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [frameError, setFrameError] = useState<string | null>(null)
  const editing = canEdit && search.get('edit') === '1'
  const [showHistory, setShowHistory] = useState(false)

  useEffect(() => {
    let cancelled = false
    getDynamicView(Number(id))
      .then((v) => !cancelled && setView(v))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      cancelled = true
    }
  }, [id])

  const [draft, setDraft] = useState<Draft | null>(null)
  useEffect(() => {
    if (view && editing) {
      setDraft({
        code: view.code,
        sources: JSON.stringify(view.sources, null, 2),
        agentTasks: JSON.stringify(view.agent_tasks ?? [], null, 2),
      })
    }
    if (!editing) setDraft(null)
  }, [view, editing])

  const draftSources = useMemo(() => parseList<DynamicViewSource>(draft?.sources, 'sources'), [draft])
  const draftTasks = useMemo(() => parseList<DeclaredTask>(draft?.agentTasks, 'agent tasks'), [draft])

  const live: FrameView | null = useMemo(() => {
    if (!view) return null
    if (draft && draftSources.ok && draftTasks.ok) {
      return { code: draft.code, sources: draftSources.value, agent_tasks: draftTasks.value }
    }
    return { code: view.code, sources: view.sources as DynamicViewSource[], agent_tasks: view.agent_tasks ?? [] }
  }, [view, draft, draftSources, draftTasks])
  // The preview redraws (and re-reads its sources) once typing pauses, not per keystroke.
  const [shown, setShown] = useState<FrameView | null>(null)
  useEffect(() => {
    if (!draft) {
      setShown(live)
      return
    }
    const t = setTimeout(() => setShown(live), 700)
    return () => clearTimeout(t)
  }, [live, draft])

  const scope: ViewScope | null = useMemo(
    () =>
      view && {
        workspace: { slug: view.workspace, name: workspaceName },
        project: view.project ? { handle: view.project, name: view.project_name ?? view.project } : null,
        view: { id: view.id, slug: view.slug, title: view.title, version: view.version },
        viewer: { email: user?.email ?? '', name: (user as { display_name?: string } | null)?.display_name ?? '' },
      },
    [view, workspaceName, user],
  )

  if (error) return <div className="p-6 text-sm text-destructive">Couldn’t load the view: {error}</div>
  if (!view || !shown || !scope) return <div className="p-6 text-sm text-muted-foreground">Loading…</div>

  const setEditing = (on: boolean) => {
    const next = new URLSearchParams(search)
    if (on) next.set('edit', '1')
    else next.delete('edit')
    setSearch(next, { replace: true })
  }

  return (
    <div className="mx-auto max-w-6xl p-6">
      <header className="mb-4 flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-xs text-muted-foreground">
            <Link className="hover:underline" to={scoped('/views')}>Views</Link>
            {' · '}
            {view.project ? (
              <Link className="hover:underline" to={scoped(`/projects/${view.project}`)}>{view.project_name ?? view.project}</Link>
            ) : (
              workspaceName
            )}
          </div>
          <h1 className="text-2xl font-semibold">{view.title}</h1>
          {view.description && <p className="max-w-prose text-sm text-muted-foreground">{view.description}</p>}
          <DeclaredTasks tasks={view.agent_tasks} className="mt-1" />
        </div>
        <div className="flex items-center gap-2 text-sm">
          <span className="text-xs text-muted-foreground">v{view.version}</span>
          <button type="button" className="rounded-md border border-border px-2.5 py-1 hover:bg-muted" onClick={() => setShowHistory((s) => !s)}>
            History
          </button>
          {canEdit && (
            <button type="button" className="rounded-md border border-border px-2.5 py-1 hover:bg-muted" onClick={() => setEditing(!editing)}>
              {editing ? 'Close editor' : 'Edit'}
            </button>
          )}
        </div>
      </header>

      {showHistory && <History view={view} canEdit={canEdit} onRestored={(v) => setView(v)} />}

      {frameError && (
        <div className="mb-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {frameError}
        </div>
      )}

      <div className={editing ? 'grid gap-4 lg:grid-cols-2' : ''}>
        {editing && draft && (
          <Editor
            draft={draft}
            sourcesError={draftSources.ok ? null : draftSources.error}
            tasksError={draftTasks.ok ? null : draftTasks.error}
            onChange={setDraft}
            onSave={async (note) => {
              if (!draftSources.ok || !draftTasks.ok) return
              const saved = await updateDynamicView(view.id, {
                code: draft.code, sources: draftSources.value, agent_tasks: draftTasks.value, note,
              })
              setView(saved)
            }}
            onDelete={async () => {
              if (!window.confirm(`Delete “${view.title}” and its history?`)) return
              await deleteDynamicView(view.id)
              navigate(scoped('/views'))
            }}
          />
        )}
        <div className="min-w-0">
          <DynamicViewFrame view={shown} scope={scope} onError={setFrameError} />
        </div>
      </div>
    </div>
  )
}

interface Draft {
  code: string
  sources: string
  agentTasks: string
}

function parseList<T>(text: string | undefined, what: string): { ok: true; value: T[] } | { ok: false; error: string } {
  if (text === undefined) return { ok: true, value: [] }
  try {
    const parsed = JSON.parse(text || '[]')
    return Array.isArray(parsed) ? { ok: true, value: parsed as T[] } : { ok: false, error: `${what} must be a JSON list` }
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) }
  }
}

function Editor({ draft, sourcesError, tasksError, onChange, onSave, onDelete }: {
  draft: Draft
  sourcesError: string | null
  tasksError: string | null
  onChange: (d: Draft) => void
  onSave: (note: string) => Promise<void>
  onDelete: () => Promise<void>
}) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <label className="flex flex-col gap-1 text-xs text-muted-foreground">
        <span>
          Code — defines <code>function View(props)</code>; the preview beside it draws this draft
        </span>
        <textarea
          className="h-[28rem] w-full rounded-md border border-input bg-background p-2 font-mono text-xs text-foreground"
          spellCheck={false}
          value={draft.code}
          onChange={(e) => onChange({ ...draft, code: e.target.value })}
        />
      </label>
      <label className="flex flex-col gap-1 text-xs text-muted-foreground">
        Sources — catalog reads made for the view, as JSON
        <textarea
          className="h-40 w-full rounded-md border border-input bg-background p-2 font-mono text-xs text-foreground"
          spellCheck={false}
          value={draft.sources}
          onChange={(e) => onChange({ ...draft, sources: e.target.value })}
        />
      </label>
      {sourcesError && <div className="text-xs text-destructive">Sources: {sourcesError}</div>}
      <label className="flex flex-col gap-1 text-xs text-muted-foreground">
        <span>
          Agent tasks — what <code>canopy.runAgentTask</code> may start, as the viewer: <code>[{'{'}"agent", "name"{'}'}]</code>
        </span>
        <textarea
          className="h-24 w-full rounded-md border border-input bg-background p-2 font-mono text-xs text-foreground"
          spellCheck={false}
          value={draft.agentTasks}
          onChange={(e) => onChange({ ...draft, agentTasks: e.target.value })}
        />
      </label>
      {tasksError && <div className="text-xs text-destructive">Agent tasks: {tasksError}</div>}
      <div className="flex flex-wrap items-center gap-2">
        <input
          className="min-w-0 flex-1 rounded-md border border-input bg-background px-2 py-1 text-sm"
          placeholder="What changed (optional)"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        <button
          type="button"
          disabled={busy || !!sourcesError || !!tasksError}
          className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
          onClick={async () => {
            setBusy(true)
            setError(null)
            try {
              await onSave(note)
              setNote('')
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e))
            } finally {
              setBusy(false)
            }
          }}
        >
          {busy ? 'Saving…' : 'Save'}
        </button>
        <button type="button" className="text-sm text-destructive hover:underline" onClick={() => onDelete().catch((e) => setError(String(e)))}>
          Delete
        </button>
      </div>
      {error && <div className="text-sm text-destructive">{error}</div>}
    </div>
  )
}

function History({ view, canEdit, onRestored }: { view: DynamicView; canEdit: boolean; onRestored: (v: DynamicView) => void }) {
  const [revs, setRevs] = useState<DynamicViewRevision[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    listDynamicViewRevisions(view.id).then(setRevs).catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [view.id, view.version])
  return (
    <div className="mb-4 rounded-lg border border-border bg-card p-3">
      {error && <div className="text-sm text-destructive">{error}</div>}
      {!revs && !error && <div className="text-sm text-muted-foreground">Loading…</div>}
      {revs && (
        <table className="w-full border-collapse text-sm">
          <tbody>
            {revs.map((r) => (
              <tr key={r.version} className="border-b border-border last:border-0">
                <td className="px-2 py-1 tabular-nums">v{r.version}</td>
                <td className="px-2 py-1">{r.note || <span className="text-muted-foreground">—</span>}</td>
                <td className="px-2 py-1 text-muted-foreground">
                  {new Date(r.changed_at).toLocaleString()}
                  {r.changed_by_email ? ` · ${r.changed_by_email}` : ''}
                </td>
                <td className="px-2 py-1 text-right">
                  {canEdit && r.version !== view.version && (
                    <button
                      type="button"
                      className="text-sm underline"
                      onClick={() => rollbackDynamicView(view.id, r.version).then(onRestored).catch((e) => setError(String(e)))}
                    >
                      Restore
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
