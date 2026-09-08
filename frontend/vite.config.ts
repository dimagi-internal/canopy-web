/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'
import { VitePWA } from 'vite-plugin-pwa'
import {
  NAVIGATE_FALLBACK_ALLOWLIST,
  NAVIGATE_FALLBACK_DENYLIST,
} from './src/pwa/navigation-fallback'

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
        // Cache the shell so the app opens instantly and survives a labs outage
        // (this is also what makes the menubar's WKWebView resilient in Phase 5).
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
        // With both false the new SW waits. The loading page keeps the precache
        // it was built against and renders; the update applies on the next
        // navigation. Still automatic — just never underneath a live page.
        skipWaiting: false,
        clientsClaim: false,
        // vite-plugin-pwa's generated SW only caches — it has no push listener.
        // Without this, a push payload arrives and nothing happens (no error,
        // no notification). importScripts (not injectManifest) keeps the
        // plugin's own precaching intact while adding push handling.
        importScripts: ['sw-push.js'],
        // Fail-safe navigate-fallback ownership (issue #345): the SW serves the
        // cached SPA shell ONLY for allowlisted SPA route prefixes; every other
        // navigation (unknown paths, Django routes, the /walkthrough/<id>/content
        // streams) goes to the network. Inverting the old "shell for everything
        // minus a denylist" default means a NEW server route can't be silently
        // swallowed. The rule + both lists live in — and are unit-tested in —
        // src/pwa/navigation-fallback.ts.
        navigateFallbackAllowlist: NAVIGATE_FALLBACK_ALLOWLIST,
        navigateFallbackDenylist: NAVIGATE_FALLBACK_DENYLIST,
        // No runtimeCaching routes registered: all non-precached fetches bypass the SW and hit
        // the network. This keeps the API uncached (stale "0 waiting" is worse than a spinner).
        // When adding entries here, take care not to match /api/ — nothing else guards against that.
        runtimeCaching: [],
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
