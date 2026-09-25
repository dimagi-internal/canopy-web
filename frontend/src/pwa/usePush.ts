import { useCallback, useEffect, useState } from 'react'
import { apiV2 } from '@/api/client.v2'

/**
 * PushManager.subscribe() wants applicationServerKey as raw bytes, but VAPID
 * keys travel as urlsafe base64. Getting this wrong throws an opaque
 * InvalidCharacterError that says nothing about the cause — hence the unit test.
 */
export function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4)
  const normalised = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/')
  // Bare global (not `window.atob`): identical in a real browser, but this
  // keeps the pure function testable under vitest's default Node environment
  // (no jsdom here — see client.v2.test.ts's "no live DOM" convention).
  const raw = atob(normalised)
  // `new Uint8Array(length)` (not `.from()`) so the result is typed
  // Uint8Array<ArrayBuffer> — applicationServerKey's BufferSource excludes a
  // SharedArrayBuffer-backed view, which `.from()` doesn't rule out under TS 5.9.
  const bytes = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
  return bytes
}

/** The app-icon count. Android supports this; a browser that doesn't just no-ops. */
export function setBadge(count: number): void {
  const nav = navigator as Navigator & {
    setAppBadge?: (n?: number) => Promise<void>
    clearAppBadge?: () => Promise<void>
  }
  if (count > 0) void nav.setAppBadge?.(count)
  else void nav.clearAppBadge?.()
}

/** Tell the server where to reach this browser. Idempotent — it upserts on endpoint. */
async function registerWithServer(sub: PushSubscription): Promise<void> {
  const json = sub.toJSON() as { endpoint?: string; keys?: { p256dh?: string; auth?: string } }
  const res = await apiV2.POST('/api/push/subscribe', {
    body: {
      endpoint: json.endpoint ?? '',
      p256dh: json.keys?.p256dh ?? '',
      auth: json.keys?.auth ?? '',
      user_agent: navigator.userAgent,
    },
  })
  if (res.error) throw new Error('could not register this device')
}

export interface UsePush {
  supported: boolean
  permission: NotificationPermission | 'unsupported'
  subscribed: boolean
  error: string | null
  subscribe: () => Promise<void>
  unsubscribe: () => Promise<void>
}

export function usePush(): UsePush {
  const supported =
    typeof window !== 'undefined' && 'serviceWorker' in navigator && 'PushManager' in window
  const [permission, setPermission] = useState<NotificationPermission | 'unsupported'>(
    supported ? Notification.permission : 'unsupported',
  )
  const [subscribed, setSubscribed] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!supported) return
    let cancelled = false
    navigator.serviceWorker.ready
      .then((reg) => reg.pushManager.getSubscription())
      .then((sub) => {
        if (cancelled) return
        setSubscribed(!!sub)
        // "Subscribed" here is the BROWSER's opinion. The server can have lost
        // its row meanwhile (pruned on a 404/410, or re-pointed to whoever
        // subscribed that endpoint last), and then the toggle reads ON while
        // nothing is ever sent. Re-registering on every open heals that: the
        // server upserts on endpoint, so a healthy row is a no-op.
        if (sub) void registerWithServer(sub).catch(() => {})
      })
      .catch(() => {
        /* no SW yet — not subscribed, not an error worth showing */
      })
    return () => {
      cancelled = true
    }
  }, [supported])

  const subscribe = useCallback(async () => {
    setError(null)
    try {
      // Ask only on an explicit click. A prompt on page load gets reflexively
      // blocked, and a block is PERMANENT — the user must dig into site settings.
      const perm = await Notification.requestPermission()
      setPermission(perm)
      if (perm !== 'granted') return

      const keyRes = await apiV2.GET('/api/push/vapid-public-key')
      if (keyRes.error || !keyRes.data) throw new Error('push is not configured on the server')

      const reg = await navigator.serviceWorker.ready
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true, // Chrome requires it; a silent push is not allowed
        applicationServerKey: urlBase64ToUint8Array(keyRes.data.public_key),
      })
      await registerWithServer(sub)
      setSubscribed(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not enable notifications')
    }
  }, [])

  const unsubscribe = useCallback(async () => {
    setError(null)
    try {
      const reg = await navigator.serviceWorker.ready
      const sub = await reg.pushManager.getSubscription()
      if (sub) {
        // Server first: if the browser drops it and we then fail to tell the
        // server, it keeps pushing at a dead endpoint until a 410 prunes it.
        const res = await apiV2.DELETE('/api/push/subscribe', { body: { endpoint: sub.endpoint } })
        if (res.error) throw new Error('could not disable notifications on the server')
        await sub.unsubscribe()
      }
      setSubscribed(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not disable notifications')
    }
  }, [])

  return { supported, permission, subscribed, error, subscribe, unsubscribe }
}
