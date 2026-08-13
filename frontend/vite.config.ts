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
      registerType: 'autoUpdate',
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
