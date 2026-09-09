/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'
import { VitePWA } from 'vite-plugin-pwa'
import { navigationMatcher } from './src/pwa/navigation-fallback'

// Same override as playwright.config/backend.sh — see E2E_API_PORT there.
const API_ORIGIN = `http://localhost:${process.env.E2E_API_PORT ?? '8000'}`

export default defineConfig({
  // Path prefix when deployed as a labs tenant (labs.connect.dimagi.com/canopy).
  // Drives import.meta.env.BASE_URL, which the router basename + API client
  // baseUrl derive from. Defaults to "/" for dev and the root deployment.
  base: process.env.VITE_BASE_PATH || '/',
  // Django's CSRF cookie name. Path-scoped per tenant on the shared labs host
  // (csrftoken_canopy) to avoid collision with sibling tenants; defaults to
  // Django's "csrftoken" for dev / the root deployment. Inlined at build time so
  // the frontend reads the right cookie. Applies in vitest too (respects define).
  define: {
    __CSRF_COOKIE_NAME__: JSON.stringify(
      process.env.VITE_CSRF_COOKIE_NAME || 'csrftoken',
    ),
  },
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      // NOT 'autoUpdate'. That preset forces skipWaiting + clientsClaim and
      // overrides the workbox options below, which is the race documented there:
      // a new SW activating underneath a page that was already served the old
      // shell, then deleting the precache out from under it. 'prompt' leaves the
      // new SW waiting; src/pwa/register.ts applies it at a safe moment.
      registerType: 'prompt',
      // The deployment is path-prefixed (/canopy/ on labs). scope and start_url
      // MUST follow it — a manifest scoped to "/" installs an app that opens the
      // wrong site. `base` is this file's own base option, computed above.
      base: process.env.VITE_BASE_PATH || '/',
      scope: process.env.VITE_BASE_PATH || '/',
      manifest: {
        name: 'Canopy Supervisor',
        short_name: 'Canopy',
        description: 'What your agent fleet needs from you.',
        // Land on the supervisor, not the workbench: this app exists to answer
        // "what's waiting on me".
        start_url: `${process.env.VITE_BASE_PATH || '/'}supervisor`,
        display: 'standalone',
        background_color: '#1c1917',
        theme_color: '#1c1917',
        icons: [
          { src: 'icons/icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'icons/icon-512.png', sizes: '512x512', type: 'image/png' },
          { src: 'icons/icon-maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
      workbox: {
        // Precache the shell so the app still opens during a labs outage (this
        // is what makes the menubar's WKWebView resilient in Phase 5). It is the
        // OFFLINE copy only — see the navigation route below for why serving it
        // to an online visitor was the bug.
        globPatterns: ['**/*.{js,css,html,svg,png,woff2}'],
        // A new SW must NOT take over a page that has already been served the
        // old shell. registerType 'autoUpdate' turns both of these on, and the
        // three defaults together are a race:
        //
        //   1. navigation is served the OLD precached index.html
        //      (navigateFallback -> createHandlerBoundToURL('index.html'))
        //   2. the new SW installs and, under skipWaiting, activates MID-LOAD
        //   3. cleanupOutdatedCaches() deletes the old precache
        //   4. clientsClaim() takes over the page
        //   5. that page now asks for the OLD asset hashes — gone from the cache
        //      and 404 on the server, because every deploy rehashes them
        //   6. the modules fail and the app paints a WHITE PAGE; a reload fixes
        //      it, because by then the new SW serves the new index.html
        //
        // Which is exactly the report: white on first load, fine after a manual
        // refresh, and it went from rare to constant on a day with six deploys.
        //
        // With both false the new SW waits, and src/pwa/registerPwa.ts adopts it
        // when the page is hidden. Still needed, but no longer urgent: step 1 is
        // gone now that navigations are network-first, so a waiting SW only means
        // a stale OFFLINE precache, not a stale page.
        skipWaiting: false,
        clientsClaim: false,
        // vite-plugin-pwa's generated SW only caches — it has no push listener.
        // Without this, a push payload arrives and nothing happens (no error,
        // no notification). importScripts (not injectManifest) keeps the
        // plugin's own precaching intact while adding push handling.
        importScripts: ['sw-push.js'],
        // Must be set EXPLICITLY, not just omitted: vite-plugin-pwa's own
        // defaults are `Object.assign({}, {navigateFallback: 'index.html'},
        // yourWorkboxOptions)`, so leaving the key out silently reinstates the
        // cache-first NavigationRoute — and it would be registered BEFORE the
        // runtimeCaching route below, which in workbox means it wins outright.
        // Verified by reading the emitted sw.js, not by reasoning about it.
        navigateFallback: undefined,
        // …and removing the NavigationRoute is not enough on its own, because
        // the PRECACHE route answers a bare `/` too: workbox defaults
        // `directoryIndex: 'index.html'`, so a request whose path ends in `/`
        // is also looked up as `<path>index.html` — which IS precached. That is
        // registered ahead of any runtimeCaching route, so `/` (the entry point
        // this bug is reported against) kept being served the stale shell while
        // `/supervisor` was already fixed. Measured in a browser: no `app-shell`
        // cache was ever created because the route never ran.
        //
        // null turns the aliasing off. `index.html` stays precached and is still
        // reachable by explicit lookup — which is exactly what the offline
        // fallback below does.
        directoryIndex: null,
        // `navigateFallback: 'index.html'` builds a
        // NavigationRoute around createHandlerBoundToURL('index.html'), which
        // answers every navigation from the PRECACHE — a copy of index.html
        // frozen at build time, naming that build's hashed assets.
        //
        // That is why the app kept needing a hard refresh. index.html is the one
        // file that must be current, because it is the only thing that says which
        // asset hashes to load; serving it from cache means a fresh visit renders
        // the PREVIOUS deploy, and only a hard refresh (which bypasses the SW
        // entirely) reaches the new one. #711 changed WHEN a new SW is adopted
        // but not WHERE the HTML comes from, so the staleness survived it — and
        // it is also the first step of the white-page race #711 describes: the
        // page can only ask for asset hashes that have been deleted if it was
        // handed a stale shell to begin with.
        //
        // So navigations are NETWORK-FIRST instead, with the precached shell
        // demoted to the offline fallback (PrecacheFallbackPlugin). Online you
        // always get the current build on the first paint, with no reload; the
        // app still opens offline, and an installed PWA / the menubar WKWebView
        // still survive a labs outage. networkTimeoutSeconds bounds a hanging
        // network so a bad connection degrades to the cached shell rather than a
        // spinner.
        //
        // Which navigations the SW handles at all is unchanged (issue #345's
        // fail-safe rule): allowlisted SPA prefixes minus denylisted server
        // routes, so a NEW server route still can't be silently swallowed. The
        // rule and both lists live in — and are unit-tested in —
        // src/pwa/navigation-fallback.ts; workbox-build inlines the matcher's
        // source into the generated SW.
        //
        // This is the ONLY runtimeCaching route, and it matches navigations
        // only. Nothing here may match /api/ — a cached "0 waiting" is worse
        // than a spinner, and nothing else guards against it.
        runtimeCaching: [
          {
            urlPattern: navigationMatcher(),
            handler: 'NetworkFirst',
            options: {
              cacheName: 'app-shell',
              networkTimeoutSeconds: 4,
              expiration: { maxEntries: 32 },
              precacheFallback: { fallbackURL: 'index.html' },
            },
          },
        ],
      },
      devOptions: { enabled: false },
    }),
  ],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  // Unit tests (vitest) live under src/. e2e/ is Playwright (`npm run test:e2e`)
  // and must be excluded here, else vitest tries to collect its specs and fails.
  test: {
    include: ['{src,packages}/**/*.{test,spec}.{ts,tsx}'],
    exclude: ['e2e/**', '**/node_modules/**', '**/dist/**'],
  },
  server: {
    port: 3000,
    proxy: {
      '/api': API_ORIGIN,
      // WebSocket control channels (realtime supervisor/turn tails, chat). Without
      // this, NO ws feature works under `vite dev` — the socket would hit :3000,
      // which has no /ws handler. ws:true upgrades the proxied connection.
      '/ws': { target: API_ORIGIN.replace('http', 'ws'), ws: true },
      '/health': API_ORIGIN,
      '/accounts': API_ORIGIN,
      '/admin': API_ORIGIN,
      '/static': API_ORIGIN,
    },
  },
})
