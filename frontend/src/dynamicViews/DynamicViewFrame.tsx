/**
 * Draws one dynamic view: frames the sandbox (`/dynamic-views/frame/`) and answers it.
 *
 * The frame's sandbox grants scripts and nothing else — never the same-origin
 * flag: beside scripts on a same-origin URL it is no sandbox at all (the guard in
 * lib/uploadedContentSandbox.test.ts holds every app iframe to that). So the
 * frame has an opaque origin, and its messages arrive with `origin === "null"`;
 * we accept one only when `event.source` is this frame's own window. Every read
 * it asks for goes through `resolveRead` (bridge.ts) and is made here, as the
 * viewer, under the view's workspace. The one thing it may make happen is an agent
 * task the view declares (`agentTasks.ts`): started here as the viewer, cancelled if
 * this frame goes away while it runs. Design: docs/architecture/dynamic-views.md.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { cancelAgentTask, createAgentTask, getAgentTask, type AgentTask } from '@/api/agentTasks'
import { apiUrl } from '@/api/base'
import { getDynamicViewGuide, type DynamicViewSource } from '@/api/dynamicViews'
import { ViewAgentTasks, type DeclaredTask } from './agentTasks'
import { resolveNavigation, resolveRead, type CatalogModel, type ReadContext } from './bridge'
import {
  CHANNEL,
  isMessage,
  type AgentTaskStatus,
  type AppContext,
  type AppOp,
  type FrameMessage,
  type HostMessage,
  type ReadRequest,
  type ViewScope,
} from './protocol'

export interface FrameView {
  code: string
  sources: DynamicViewSource[]
  /** The agent tasks the code may start (the view's `agent_tasks`); none when absent. */
  agent_tasks?: readonly DeclaredTask[]
}

/** What the frame is told about a task: its progress, not the request it was sent. */
function statusOf(t: AgentTask): AgentTaskStatus {
  return {
    id: t.id, agent: t.agent, name: t.name, status: t.status, error: t.error ?? '',
    ...(t.status === 'done' ? { result: t.result ?? null } : {}),
    turn_id: t.turn_id ?? null, deadline_at: t.deadline_at, created_at: t.created_at, finished_at: t.finished_at ?? null,
  }
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

interface Props {
  view: FrameView
  scope: ViewScope
  /** Called with the frame's compile or runtime error, or null once it draws again. */
  onError?: (message: string | null) => void
  /**
   * A workspace app's page (apps/workspace_apps). `load` replaces the catalog
   * reads of `view.sources` (an app reads its own collections, through its own
   * routes); `call` answers the page's query/act/history, `go` its moves between
   * pages. Without it the frame is a plain dynamic view and every app call is refused.
   */
  app?: {
    context: AppContext
    load: () => Promise<Record<string, unknown>>
    call: (op: AppOp) => Promise<unknown>
    go: (page: string, params: Record<string, string>) => void
    /** May the page use canopy.read (the dynamic-view catalog)? Never on the public door. */
    canopyReads: boolean
  }
}

async function readAs(url: string): Promise<unknown> {
  const res = await fetch(apiUrl(url), { credentials: 'same-origin', headers: { Accept: 'application/json' } })
  const text = await res.text()
  let body: unknown = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = null
  }
  if (!res.ok) {
    const detail = body && typeof body === 'object' ? (body as { detail?: unknown; title?: unknown }).detail ?? (body as { title?: unknown }).title : null
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${res.status}`)
  }
  return body
}

export async function readSources(catalog: CatalogModel[], ctx: ReadContext, sources: DynamicViewSource[]) {
  const entries = await Promise.all(
    (sources ?? []).map(async (s) => {
      const r = resolveRead(catalog, ctx, { model: s.model, params: s.params, path: s.path })
      if (!r.ok) return [s.alias, { error: r.error }] as const
      try {
        return [s.alias, await readAs(r.url)] as const
      } catch (e) {
        return [s.alias, { error: e instanceof Error ? e.message : String(e) }] as const
      }
    }),
  )
  return Object.fromEntries(entries) as Record<string, unknown>
}

function theme(): 'light' | 'dark' {
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light'
}

export function DynamicViewFrame({ view, scope, onError, app }: Props) {
  const frame = useRef<HTMLIFrameElement>(null)
  const loads = useRef(0)
  const [height, setHeight] = useState(240)
  const [gone, setGone] = useState(false)
  const navigate = useNavigate()
  const ctx: ReadContext = { workspace: scope.workspace.slug, project: scope.project?.handle ?? null }
  const ctxKey = `${ctx.workspace}/${ctx.project ?? ''}`
  // Read through a ref so a draft's declaration (the editor's preview) applies to
  // the next call; the workspace is the view's and does not change under a frame.
  const declared = useRef<readonly DeclaredTask[]>(view.agent_tasks ?? [])
  declared.current = view.agent_tasks ?? []
  const tasks = useRef<ViewAgentTasks | null>(null)
  if (tasks.current === null) {
    const ws = ctx.workspace
    tasks.current = new ViewAgentTasks(
      {
        create: (body) => createAgentTask(ws, body),
        get: (id, waitS, signal) => getAgentTask(ws, id, waitS, signal),
        cancel: (id) => cancelAgentTask(ws, id),
      },
      () => declared.current,
    )
  }
  // Leaving the page (or the frame) cancels whatever the view is still waiting on.
  useEffect(() => () => tasks.current?.cancelAll(), [])

  const post = useCallback((msg: HostMessage) => {
    // An opaque-origin frame can only be addressed as "*"; the window we post to
    // is our own frame's, and the `gone` check stops us talking to a document it
    // navigated to.
    frame.current?.contentWindow?.postMessage(msg, '*')
  }, [])

  const load = useCallback(async () => {
    if (app) return app.load()
    const guide = await getDynamicViewGuide()
    return readSources(guide.catalog, ctx, view.sources)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [app, view.sources, ctxKey])

  const draw = useCallback(async () => {
    const data = await load()
    post({ channel: CHANNEL, type: 'render', code: view.code, data, scope, theme: theme(), app: app?.context })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [post, load, view.code, ctxKey, scope.view.version, app?.context])

  useEffect(() => {
    async function onMessage(ev: MessageEvent) {
      if (!frame.current || ev.source !== frame.current.contentWindow || gone) return
      const msg = ev.data as FrameMessage
      if (!isMessage(msg)) return
      switch (msg.type) {
        case 'ready':
          onError?.(null)
          draw().catch((e) => onError?.(e instanceof Error ? e.message : String(e)))
          return
        case 'refresh': {
          post({ channel: CHANNEL, type: 'data', data: await load() })
          return
        }
        case 'app': {
          if (!app) {
            post({ channel: CHANNEL, type: 'read-result', id: msg.id, ok: false, error: 'this is a view, not an app: it cannot query or act' })
            return
          }
          try {
            post({ channel: CHANNEL, type: 'read-result', id: msg.id, ok: true, data: await app.call(msg.request) })
          } catch (e) {
            post({ channel: CHANNEL, type: 'read-result', id: msg.id, ok: false, error: e instanceof Error ? e.message : String(e) })
          }
          return
        }
        case 'go':
          if (app && typeof msg.page === 'string') app.go(msg.page, msg.params ?? {})
          return
        case 'read': {
          if (app && !app.canopyReads) {
            post({ channel: CHANNEL, type: 'read-result', id: msg.id, ok: false, error: 'a public page reads only its own app' })
            return
          }
          const guide = await getDynamicViewGuide()
          const r = resolveRead(guide.catalog, ctx, msg.request as ReadRequest)
          if (!r.ok) {
            post({ channel: CHANNEL, type: 'read-result', id: msg.id, ok: false, error: r.error })
            return
          }
          try {
            post({ channel: CHANNEL, type: 'read-result', id: msg.id, ok: true, data: await readAs(r.url) })
          } catch (e) {
            post({ channel: CHANNEL, type: 'read-result', id: msg.id, ok: false, error: e instanceof Error ? e.message : String(e) })
          }
          return
        }
        case 'agent-task': {
          const id = msg.id
          tasks.current!.run(msg.request, (t) => post({ channel: CHANNEL, type: 'agent-task-status', id, task: statusOf(t) }))
            .then((data) => post({ channel: CHANNEL, type: 'read-result', id, ok: true, data }))
            .catch((e) => post({ channel: CHANNEL, type: 'read-result', id, ok: false, error: message(e) }))
          return
        }
        case 'agent-task-read': {
          const id = msg.id
          tasks.current!.read(msg.taskId)
            .then((t) => post({ channel: CHANNEL, type: 'read-result', id, ok: true, data: statusOf(t) }))
            .catch((e) => post({ channel: CHANNEL, type: 'read-result', id, ok: false, error: message(e) }))
          return
        }
        case 'navigate': {
          const to = resolveNavigation(ctx.workspace, msg.path)
          if (to.kind === 'app') navigate(to.to)
          else if (to.kind === 'external' && window.confirm(`This view wants to open ${to.url}`)) {
            window.open(to.url, '_blank', 'noopener,noreferrer')
          }
          return
        }
        case 'resize':
          if (Number.isFinite(msg.height)) setHeight(Math.min(Math.max(Math.round(msg.height), 80), 20000))
          return
        case 'error':
          onError?.(String(msg.message ?? 'the view failed'))
          return
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draw, load, post, navigate, gone, ctxKey, view.sources, app])

  // A changed view (an edit, a restored version) redraws in the same frame.
  useEffect(() => {
    if (loads.current > 0 && !gone) draw().catch((e) => onError?.(e instanceof Error ? e.message : String(e)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view.code, view.sources, app?.context])

  if (gone) {
    return (
      <div className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
        This view navigated its frame away, so canopy stopped talking to it. Reload the page to draw it again.
      </div>
    )
  }
  return (
    <iframe
      ref={frame}
      title="Dynamic view"
      src={apiUrl(`/dynamic-views/frame/?theme=${theme()}`)}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      className="block w-full border-0"
      style={{ height }}
      onLoad={() => {
        loads.current += 1
        // The first load is the frame; any later one means the view's code
        // replaced the document. Stop answering it.
        if (loads.current > 1) {
          tasks.current?.cancelAll()
          setGone(true)
        }
      }}
    />
  )
}
