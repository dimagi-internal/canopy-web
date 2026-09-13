import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router-dom'

import { apiV2 } from '@/api/client.v2'
import { API_BASE, apiUrl } from '@/api/base'
import { buildPageContext } from './pageContext'

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

interface WidgetHandle {
  destroy(): void
  provideContext(fn: () => unknown): void
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
  const pathRef = useRef(location.pathname)
  pathRef.current = location.pathname
  const handleRef = useRef<WidgetHandle | null>(null)

  useEffect(() => {
    let cancelled = false

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
        ...(config.agent ? { agent: config.agent } : {}),
        mode: 'overlay',
        launcherLabel: 'Ask canopy',
      })
      // Read at conversation-open, so this closure sees whatever page the user
      // is on then — not the one they were on when the widget mounted.
      handle.provideContext(() => buildPageContext(pathRef.current))
      handleRef.current = handle
    }

    void mount().catch(() => {
      // A widget that fails to mount must not take the app down with it. The
      // absence is the signal; /api/embed/self says whether it should be here.
    })

    return () => {
      cancelled = true
      handleRef.current?.destroy()
      handleRef.current = null
    }
  }, [])

  return null
}
