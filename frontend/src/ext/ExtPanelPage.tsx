/**
 * `/ext/panel/` — the Dimagi Canopy browser extension's side panel (the extension frames
 * this page; the server lets only the allowed extension origins do so,
 * apps/embedding/extension.py). It asks which of the person's apps have a page for the
 * tab in front of them (`GET /api/browser/apps`), offers a picker when several do, and
 * draws the chosen one in the same sandboxed frame an app page gets on canopy, with
 * `canopy.browser` relayed through the gate in `extensionBridge.ts`.
 * Design: docs/architecture/browser-extension.md.
 *
 * A page with `layout: "full"` fills the panel below the picker (shown only when several apps match),
 * with no padding, its frame scrolling itself. The panel has no address bar: `canopy.page.setParams`
 * only updates the page's own `app.params`.
 *
 * Signed out it cannot send the person to Google (a framed sign-in is refused), so it
 * asks the extension to open the sign-in in a tab and checks again when the panel gets
 * focus, the tab changes, or the person presses the button.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getMe, type MeOut } from '@/api/me'
import { getInstalledApp, listBrowserApps, type BrowserApp, type InstalledApp } from '@/api/workspaceApps'
import { loginHref } from '@/auth/loginHref'
import { CanopyWordmark } from '@/brand/CanopyWordmark'
import type { BrowserHost } from '@/dynamicViews/DynamicViewFrame'
import type { DeclaredConnection } from '@/dynamicViews/connectionCalls'
import { AppFrame } from '@/workspaceApps/AppFrame'
import { scopedPath } from '@/lib/scopedLinks'
import { BrowserGate, ExtensionLink, allowedOrigins, visibleContext, type Context } from './extensionBridge'

/** Where the sign-in tab lands: a page that says to come back here. */
export const SIGNED_IN_PATH = '/ext/signed-in/'
const RECHECK_MS = 2_000

type Session = { kind: 'checking' } | { kind: 'out' } | { kind: 'in'; me: MeOut }

function appId(a: Pick<BrowserApp, 'workspace' | 'app_key' | 'page_slug'>): string {
  return `${a.workspace}/${a.app_key}/${a.page_slug}`
}

export function signInUrl(origin: string = window.location.origin): string {
  return new URL(loginHref(SIGNED_IN_PATH), origin).toString()
}

function useExtensionLink() {
  const [context, setContext] = useState<Context | null>(null)
  const listeners = useRef(new Set<(event: string, data: unknown, tabId: number, adapter: string) => void>())
  const origins = useMemo(() => allowedOrigins(), [])
  const framed = typeof window !== 'undefined' && window.parent !== window
  const link = useMemo(() => {
    if (!framed || !origins.length) return null
    const ancestor = (window.location as Location & { ancestorOrigins?: DOMStringList }).ancestorOrigins?.[0] ?? null
    return new ExtensionLink(origins, {
      parent: window.parent,
      ancestor,
      post: (msg, origin) => window.parent.postMessage(msg, origin),
    }, {
      context: (c) => setContext(c),
      event: (e) => listeners.current.forEach((l) => l(e.event, e.data, e.tabId, e.adapter)),
    })
  }, [framed, origins])

  useEffect(() => {
    if (!link) return
    const onMessage = (ev: MessageEvent) => {
      link.receive({ source: ev.source, origin: ev.origin, data: ev.data })
    }
    window.addEventListener('message', onMessage)
    link.ready()
    return () => {
      window.removeEventListener('message', onMessage)
      link.close()
    }
  }, [link])

  return { link, context, listeners }
}

export function ExtPanelPage() {
  const { link, context, listeners } = useExtensionLink()
  const [session, setSession] = useState<Session>({ kind: 'checking' })
  const lastCheck = useRef(0)

  const check = useCallback(async () => {
    lastCheck.current = Date.now()
    const me = await getMe().catch(() => null)
    setSession(me ? { kind: 'in', me } : { kind: 'out' })
  }, [])

  useEffect(() => {
    void check()
  }, [check])

  // Signed out: look again whenever the person comes back to the panel or the tab changes.
  useEffect(() => {
    if (session.kind !== 'out') return
    const again = () => {
      if (Date.now() - lastCheck.current > RECHECK_MS) void check()
    }
    window.addEventListener('focus', again)
    document.addEventListener('visibilitychange', again)
    return () => {
      window.removeEventListener('focus', again)
      document.removeEventListener('visibilitychange', again)
    }
  }, [session.kind, check])
  useEffect(() => {
    if (session.kind === 'out' && Date.now() - lastCheck.current > RECHECK_MS) void check()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [context])

  if (!link) {
    return (
      <Shell>
        <p className="text-sm text-muted-foreground">
          This page runs inside the Dimagi Canopy browser extension&apos;s side panel.
        </p>
      </Shell>
    )
  }
  if (session.kind === 'checking') return <Shell><p className="text-sm text-muted-foreground">Loading…</p></Shell>
  if (session.kind === 'out') {
    return (
      <Shell>
        <p className="mb-4 text-sm text-muted-foreground">Sign in to see your apps for this tab.</p>
        <button type="button" className="w-full rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          onClick={() => link.open(signInUrl())}>
          Sign in to Canopy
        </button>
        <button type="button" className="mt-2 w-full rounded-md border border-border px-3 py-2 text-sm hover:bg-muted"
          onClick={() => void check()}>
          I&apos;ve signed in — check again
        </button>
      </Shell>
    )
  }
  return <SignedInPanel link={link} context={context} listeners={listeners} me={session.me} />
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-background p-4 text-foreground">
      <div className="mb-4 text-lg font-semibold"><CanopyWordmark /></div>
      {children}
    </div>
  )
}

interface SignedInProps {
  link: ExtensionLink
  context: Context | null
  listeners: React.MutableRefObject<Set<(event: string, data: unknown, tabId: number, adapter: string) => void>>
  me: MeOut
}

function SignedInPanel({ link, context, listeners, me }: SignedInProps) {
  const url = context?.tab?.url ?? null
  const [apps, setApps] = useState<BrowserApp[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [chosen, setChosen] = useState<string | null>(null)

  useEffect(() => {
    if (!url) {
      setApps([])
      return
    }
    let cancelled = false
    listBrowserApps(url).then(
      (rows) => {
        if (!cancelled) {
          setApps(rows)
          setError(null)
        }
      },
      (e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      },
    )
    return () => {
      cancelled = true
    }
  }, [url])

  const current = apps?.find((a) => appId(a) === chosen) ?? apps?.[0] ?? null

  if (error) return <Shell><p className="text-sm text-destructive">{error}</p></Shell>
  if (!context) return <Shell><p className="text-sm text-muted-foreground">Waiting for the extension…</p></Shell>
  if (apps === null) return <Shell><p className="text-sm text-muted-foreground">Loading…</p></Shell>
  if (!current) {
    return (
      <Shell>
        <p className="text-sm text-muted-foreground">None of your apps has a page for this tab.</p>
      </Shell>
    )
  }
  return (
    <div className="flex h-screen flex-col bg-background text-foreground">
      {apps.length > 1 && (
        <div className="shrink-0 border-b border-border p-2">
          <label className="sr-only" htmlFor="ext-app-picker">App</label>
          <select id="ext-app-picker" className="w-full rounded-md border border-border bg-card px-2 py-1 text-sm"
            value={appId(current)} onChange={(e) => setChosen(e.target.value)}>
            {apps.map((a) => (
              <option key={appId(a)} value={appId(a)}>{a.app_name} · {a.page_title} ({a.workspace_name})</option>
            ))}
          </select>
        </div>
      )}
      <PanelApp key={appId(current)} entry={current} apps={apps} link={link} context={context} listeners={listeners} me={me} />
    </div>
  )
}

interface PanelAppProps extends SignedInProps {
  entry: BrowserApp
  apps: BrowserApp[]
}

function PanelApp({ entry, apps, link, context, listeners, me }: PanelAppProps) {
  const [app, setApp] = useState<InstalledApp | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pageSlug, setPageSlug] = useState(entry.page_slug)
  const [params, setParams] = useState<Record<string, string>>({})

  useEffect(() => {
    getInstalledApp(entry.workspace, entry.app_key).then(setApp, (e) => setError(e instanceof Error ? e.message : String(e)))
  }, [entry.workspace, entry.app_key])

  // The declaration of the page on screen, as the server listed it for this tab; none → no canopy.browser.
  const decl = apps.find((a) => a.workspace === entry.workspace && a.app_key === entry.app_key && a.page_slug === pageSlug) ?? null
  const contextListeners = useRef(new Set<(visible: unknown) => void>())
  const host: BrowserHost | undefined = useMemo(() => {
    if (!decl) return undefined
    // The gate reads the tab as the extension last described it (`link.context`), on every call.
    const gate = new BrowserGate(decl, link)
    return {
      info: { adapter: decl.adapter, capabilities: [...decl.capabilities], events: [...decl.events] },
      context: () => gate.context(),
      request: (capability, args) => gate.request(capability, args),
      open: (url) => gate.open(url),
      subscribe(listener) {
        const onEvent = (event: string, data: unknown, tabId: number, adapter: string) => {
          if (gate.admits({ event, data, tabId, adapter })) listener(event, data)
        }
        const onContext = (visible: unknown) => listener('context', visible)
        listeners.current.add(onEvent)
        contextListeners.current.add(onContext)
        return () => {
          listeners.current.delete(onEvent)
          contextListeners.current.delete(onContext)
        }
      },
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [decl?.workspace, decl?.app_key, decl?.page_slug, link])

  // A change of tab reaches the page as the `context` event (what it may see of the new one).
  const seen = useRef<string>('')
  useEffect(() => {
    if (!decl) return
    const visible = visibleContext(decl, context)
    const key = JSON.stringify(visible)
    if (key === seen.current) return
    seen.current = key
    contextListeners.current.forEach((l) => l(visible))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [context, decl?.page_slug])

  if (error) return <p className="p-4 text-sm text-destructive">{error}</p>
  if (!app) return <p className="p-4 text-sm text-muted-foreground">Loading…</p>
  const page = app.pages.find((p) => p.slug === pageSlug && p.audience === 'member')
  if (!page) return <p className="p-4 text-sm text-muted-foreground">This page is not available to you.</p>
  const full = page.layout === 'full'
  return (
    <div className={full ? 'relative min-h-0 flex-1 overflow-hidden' : 'min-h-0 flex-1 overflow-auto p-2'}>
      <AppFrame
        fill={full}
        onParams={() => undefined}
        key={`${page.slug}?${JSON.stringify(params)}`}
        workspace={{ slug: entry.workspace, name: entry.workspace_name }}
        app={{ key: app.key, name: app.name }}
        page={page}
        roles={[...app.my_roles]}
        params={params}
        surface="member"
        dataModel={app.data_model as Record<string, unknown>}
        settings={app.settings as Record<string, unknown>}
        connections={app.connections as unknown as DeclaredConnection[] | undefined}
        viewer={{ email: me.email, name: me.name }}
        onGo={(slug, p) => {
          if (app.pages.some((x) => x.slug === slug && x.audience === 'member')) {
            setPageSlug(slug)
            setParams(p)
          }
        }}
        // A bundle page opening another in a new tab gets the full canopy page, in a browser tab.
        pageHref={(slug, p) => {
          const qs = new URLSearchParams(p).toString()
          return `${window.location.origin}${import.meta.env.BASE_URL.replace(/\/$/, '')}${scopedPath(entry.workspace, `/apps/${app.key}/${slug}${qs ? `?${qs}` : ''}`)}`
        }}
        pages={app.pages}
        browser={host}
      />
    </div>
  )
}
