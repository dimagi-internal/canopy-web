// Injected into the generated service worker (vite-plugin-pwa's injectManifest
// would replace the whole SW; importScripts keeps its precaching intact).
//
// Without a 'push' listener the payload arrives and NOTHING happens — no error,
// no notification, just silence. That is the single most confusing way for push
// to be broken, so this file is small and does exactly two things.

self.addEventListener('push', (event) => {
  let data = {}
  try {
    data = event.data ? event.data.json() : {}
  } catch {
    data = {}
  }
  // event.data.json() returns `null` (not a throw) for a literal `null` body.
  // Coerce any non-object result to {} so `data.title` etc. below can't throw
  // synchronously, before event.waitUntil is even reached.
  if (!data || typeof data !== 'object') data = {}
  const title = data.title || 'Canopy'
  event.waitUntil(
    self.registration.showNotification(title, {
      body: data.body || '',
      icon: 'icons/icon-192.png',
      badge: 'icons/icon-192.png',
      // Collapse to one notification per agent: five separate buzzes for the
      // same agent is how you get muted.
      tag: title,
      renotify: true,
      data: { url: data.url || 'supervisor' },
    }),
  )
  // Keep the app-icon badge current even while the app is closed.
  // SupervisorPage.setBadge only runs on mount, so a push that arrives with
  // the app closed would otherwise leave the badge stale until next open.
  // Guarded on both sides: the Badging API may not exist in this SW (only
  // navigator, not self.registration, exposes it), and a throw here would
  // otherwise silently swallow the whole 'push' listener — the one failure
  // this handler exists to prevent — so only call it when data.count is a
  // number.
  if (typeof data.count === 'number' && self.navigator && self.navigator.setAppBadge) {
    event.waitUntil(self.navigator.setAppBadge(data.count).catch(() => {}))
  }
})

// Resolve a (possibly app-relative) notification URL against the deployment's
// mount point. The deployment can be path-prefixed (e.g. /canopy/), and
// self.registration.scope carries that prefix (self.location.origin/href do
// not survive a leading slash — new URL('/x', anyBase) always drops the
// base's path). Stripping the leading slash keeps the value from overriding
// the scope's path component.
function resolveNotificationUrl(raw) {
  const path = String(raw || 'supervisor').replace(/^\/+/, '')
  return new URL(path, self.registration.scope).href
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const target = resolveNotificationUrl(event.notification.data && event.notification.data.url)
  const scope = self.registration.scope
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((cls) => {
      // A target off this app (an AWS sign-in approval page) gets its own
      // window. Navigating the person's open canopy tab away to it would lose
      // whatever they had open there.
      if (!target.startsWith(scope)) return self.clients.openWindow(target)
      // Focus an existing tab of this app rather than stacking another
      // window. Match by scope (not exact URL) since a tab may sit on any
      // in-app route (?query params, /agents/echo, etc.) — then navigate it
      // to the target if it isn't already there.
      for (const c of cls) {
        if (c.url.startsWith(scope) && 'focus' in c) {
          return c.focus().then(() => {
            if (c.url !== target && 'navigate' in c) return c.navigate(target)
            return c
          })
        }
      }
      return self.clients.openWindow(target)
    }),
  )
})
