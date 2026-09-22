import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router-dom'

import { apiV2 } from '@/api/client.v2'
import { API_BASE, CSRF_COOKIE_NAME, apiUrl } from '@/api/base'
import { buildPageContext } from './pageContext'
import { currentPageState, hasPageState, onPageStateChanged } from './pageState'
import { resourceChanged } from './pageInvalidation'
import { useTheme } from '@/theme/ThemeProvider'
import { currentSpecs, onPageActionsChanged, runPageAction } from './pageActions'

/**
 * canopy-web embedding its own widget.
 *
 * Why bother, when canopy already has first-class chat at `/w/:ws/chat`: the
 * reason to talk to an agent is usually about what is in front of you, and
 * that chat page is not in front of you. Two real cases drove this — an agent
 * inbox that has gone stale, and a feature set you decide to deprecate while
 * looking at it and have forgotten a minute later. Both are cheap to say and
 * expensive to reconstruct, and both happen on a canopy page.
 *
 * It is NOT a substitute for embedding in a real third-party host. The frame
 * here is same-origin, so none of the origin discipline (`targetOrigin`,
 * `event.origin` rejection, storage partitioning) is exercised. What it does
 * exercise is everything above that: the handshake, the agent picker, session
 * creation and history, the context snapshot, and whether the panel is
 * actually pleasant to use.
 *
 * Mounted inside the authenticated shell only. Public surfaces (`/share/…`,
 * `/storyboard/…`, `/invite/…`) have no signed-in user to mint a token for,
 * and a launcher on a chrome-less public viewer would be wrong anyway.
 */

interface ActionOptions {
  description?: string
  parameters?: Record<string, unknown>
}

// Structural, because the widget arrives as a script tag rather than an import
// — so this is a second declaration of `canopy-widget`'s own handle and the
// two can drift. Keep them in step; `widget.test.ts` exercises the real one.
interface WidgetHandle {
  destroy(): void
  provideContext(fn: () => unknown): void
  setPageState(state: Record<string, unknown>): void
  registerAction(
    name: string,
    run: (args: Record<string, unknown>) => unknown | Promise<unknown>,
    options?: ActionOptions,
  ): void
  unregisterAction(name: string): void
  /** Optional in the type because the loader is fetched as a script: a stale
   *  cached copy from before theming existed must degrade, not throw. */
  setTheme?(theme: { mode?: 'light' | 'dark' | 'auto' }): void
  setLauncherVisible?(visible: boolean): void
}

interface CanopyGlobal {
  init(options: Record<string, unknown>): WidgetHandle
}

declare global {
  interface Window {
    canopy?: CanopyGlobal
  }
}

interface SelfConfig {
  enabled: boolean
  app: string
  agent: string
}

/** Loaded once per page load. The loader is served with `no-cache`, so a new
 *  deploy is picked up on the next navigation without a version to bump. */
function loadLoader(src: string): Promise<void> {
  const existing = document.querySelector<HTMLScriptElement>(`script[data-canopy-widget-loader]`)
  if (existing) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const script = document.createElement('script')
    script.src = src
    script.async = true
    script.dataset.canopyWidgetLoader = ''
    script.onload = () => resolve()
    script.onerror = () => reject(new Error(`could not load ${src}`))
    document.head.appendChild(script)
  })
}

export function CanopyWidget() {
  const location = useLocation()
  // The widget is created once and lives across navigations — recreating it on
  // every route change would close an open conversation mid-sentence. The path
  // is read through a ref instead, so context is always current.
  // Path AND query: the search string is where a filtered view keeps its
  // state, so dropping it made "/insights?project=x" indistinguishable from
  // the unfiltered page.
  const pathRef = useRef(location.pathname)
  pathRef.current = location.pathname
  const searchRef = useRef(location.search)
  searchRef.current = location.search
  const handleRef = useRef<WidgetHandle | null>(null)
  // canopy is a host like any other, and its pages have a light/dark toggle.
  // The panel used to be dark regardless, so switching canopy to light left a
  // dark assistant floating on it. Read through a ref inside the mount effect,
  // which deliberately runs once; the effect below follows later toggles.
  const { theme } = useTheme()
  const themeRef = useRef(theme)
  themeRef.current = theme
  /** Names currently mirrored into the widget, so a withdrawn action is
   *  actually withdrawn rather than left callable. */
  const declared = useRef<Set<string>>(new Set())

  useEffect(() => {
    let cancelled = false
    let unsubscribe: (() => void) | null = null
    let unsubscribeState: (() => void) | null = null

    async function mount() {
      const { data, response } = await apiV2.GET('/api/embed/self')
      if (!response.ok || !data) return
      const config = data as SelfConfig
      if (cancelled || !config.enabled || handleRef.current) return

      await loadLoader(apiUrl('/embed/widget.js'))
      if (cancelled || !window.canopy || handleRef.current) return

      const handle = window.canopy.init({
        // Same origin, so a bare path prefix is right — and it keeps the
        // WebSocket URL builder on the same branch the deployed app uses.
        baseUrl: API_BASE,
        app: config.app,
        tokenUrl: apiUrl('/api/embed/token'),
        // Not `csrftoken`: the /canopy labs tenant path-scopes the cookie so it
        // cannot collide with its sibling apps on the shared host. Left at the
        // default the mint 403s and the widget never starts.
        csrfCookieName: CSRF_COOKIE_NAME,
        ...(config.agent ? { agent: config.agent } : {}),
        mode: 'overlay',
        // The HOST names its own launcher; canopy is a host like any other.
        launcherLabel: 'Canopy AI',
        // Mode only: canopy's accent IS the widget's default accent, so there
        // is nothing to override — and passing it would pin today's orange in
        // two places.
        theme: { mode: themeRef.current },
        // canopy says a resource moved; the page decides what re-reading means.
        // Registered at init rather than as a later call because a notification
        // that arrives before the handler exists is simply lost — and the
        // window between mount and first render is exactly when a fleet turn
        // might land.
        onInvalidate: (resource: string) => resourceChanged(resource),
      })
      // Read at conversation-open, so this closure sees whatever page the user
      // is on then — not the one they were on when the widget mounted.
      // The context block that rides the FIRST message carries the page STATE
      // too, not just the route.
      //
      // Found live on 2026-09-16: the agent replied "the canopy-web MCP server
      // is still connecting, its tools aren't loaded" and could not call
      // `current_page` — so all it had was `{surface, path}`. It refused to
      // guess, correctly, and was blind to the twenty rows on screen.
      //
      // MCP servers connect asynchronously, and the widget creates a FRESH
      // session per conversation — so the runner spawns a new process and the
      // first turn races that connection. The first turn is also the one
      // carrying the user's actual question, which made the most important turn
      // depend on the flakiest link.
      //
      // The state is already computed and already pushed; it simply was not in
      // the prompt. Including it makes the first turn work whether or not MCP
      // is up, and leaves `current_page` doing what it is actually good at:
      // RE-READING later, when the page has moved.
      handle.provideContext(() => ({
        ...buildPageContext(pathRef.current, searchRef.current),
        ...(hasPageState() ? currentPageState() : {}),
      }))

      // The state channel, beside the context pull above. `provideContext` is
      // answered once when a conversation opens; this is pushed whenever the
      // page's view changes, which is what keeps "the ones I'm looking at"
      // true after the user filters mid-conversation.
      //
      // The route layer rides along on every push, so a page that declares
      // state does not have to restate where it is — and a page that declares
      // none stays silent rather than announcing an empty screen.
      const pushState = (state: Record<string, unknown>) => {
        handle.setPageState({
          ...buildPageContext(pathRef.current, searchRef.current),
          ...state,
        })
      }
      if (hasPageState()) pushState(currentPageState())
      unsubscribeState = onPageStateChanged(pushState)

      // Mirror the page's registry into the widget, and keep mirroring as the
      // user navigates. Declared with SCHEMAS, because an agent that knows an
      // action exists but not how to call it is barely better off than one
      // that does not know at all.
      const sync = (specs: ReturnType<typeof currentSpecs>) => {
        for (const name of declared.current) {
          if (!specs.some((s) => s.name === name)) handle.unregisterAction(name)
        }
        declared.current = new Set(specs.map((s) => s.name))
        for (const spec of specs) {
          handle.registerAction(
            spec.name,
            // Resolved at CALL time, not registration: the page that answers
            // must be the one on screen now, not the one that was mounted when
            // the widget started.
            (args) => runPageAction(spec.name, args),
            { description: spec.description, parameters: spec.parameters },
          )
        }
      }
      sync(currentSpecs())
      unsubscribe = onPageActionsChanged(sync)

      handleRef.current = handle
      // The route effect ran before the handle existed; apply its answer now,
      // or a widget first mounted on a chat page would show its launcher there.
      handle.setLauncherVisible?.(!onChatPageRef.current)
    }

    void mount().catch(() => {
      // A widget that fails to mount must not take the app down with it. The
      // absence is the signal; /api/embed/self says whether it should be here.
    })

    return () => {
      cancelled = true
      unsubscribe?.()
      unsubscribeState?.()
      handleRef.current?.destroy()
      handleRef.current = null
    }
  }, [])

  // No launcher on a chat page. You are already talking to an agent there, and
  // the bubble sat directly on top of the composer's Send button — the one
  // control that page exists for. The widget itself is not torn down (it lives
  // across navigations so an open conversation is not cut off); only its
  // launcher is hidden, and it comes back on the next non-chat route.
  // Deliberately `location.pathname` in the deps, so it re-evaluates per route;
  // before the handle exists this is a no-op and the mount effect applies it.
  const onChatPage = isChatRoute(location.pathname)
  const onChatPageRef = useRef(onChatPage)
  onChatPageRef.current = onChatPage
  useEffect(() => {
    handleRef.current?.setLauncherVisible?.(!onChatPage)
  }, [onChatPage])

  // Follow canopy's own toggle while the widget is up. A no-op until the mount
  // effect has created the handle; init already carried the mode at that point.
  useEffect(() => {
    handleRef.current?.setTheme?.({ mode: theme })
  }, [theme])

  return null
}

/** A chat session page, or the chat home: `/w/:workspace/chat[/:id]`. Exported
 *  for its test — a route that is missed here puts the bubble back over a Send
 *  button. */
export function isChatRoute(pathname: string): boolean {
  return /\/w\/[^/]+\/chat(\/|$)/.test(pathname)
}
