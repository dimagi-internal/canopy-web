#!/usr/bin/env node
/**
 * Assert the GENERATED service worker cannot take over a live page.
 *
 * WHY THIS CHECKS THE OUTPUT AND NOT THE CONFIG. The white-page bug was not a
 * wrong setting — it was a setting that silently did nothing. `vite.config.ts`
 * carried `workbox: { skipWaiting: false, clientsClaim: false }` and the built
 * sw.js emitted both anyway, because vite-plugin-pwa overwrites them whenever
 * `registerType` is 'autoUpdate':
 *
 *     // vite-plugin-pwa/dist/index.js
 *     if ((injectRegister === "auto" || injectRegister == null)
 *         && registerType === "autoUpdate") {
 *       workbox.skipWaiting = true
 *       workbox.clientsClaim = true
 *     }
 *
 * So a test asserting the CONFIG would have passed while production shipped the
 * bug, and would keep passing if a future plugin release moved that override to
 * another preset, or if someone flipped `registerType` back. The only artifact
 * that cannot lie about what ships is the file that ships. This reads it.
 *
 * WHAT IT FORBIDS, and why each one matters to the race:
 *
 *   clientsClaim()      — an activating SW seizing control of documents that
 *                         were already served the PREVIOUS shell. This is the
 *                         step that points a live page at a precache it was not
 *                         built against.
 *   bare skipWaiting()  — activating without waiting for those documents to go
 *                         away. Every call must sit behind the SKIP_WAITING
 *                         message, i.e. happen only when the page ASKS for it,
 *                         which `registerPwa.ts` does only while hidden.
 *
 * `cleanupOutdatedCaches()` and `createHandlerBoundToURL()` stay and are not
 * checked: both are correct once activation is under the page's control, and
 * banning them would forbid offline support rather than the race.
 *
 * Runs as part of `npm run build`, so the existing CI "Frontend build" job
 * covers it with no new workflow.
 */
import { readFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const dist = join(dirname(dirname(fileURLToPath(import.meta.url))), 'dist')
const swPath = join(dist, 'sw.js')

const fail = (msg) => {
  console.error(`\n✖ service-worker shape check failed\n\n${msg}\n`)
  process.exit(1)
}

if (!existsSync(swPath)) {
  fail(
    `No ${swPath}.\n` +
      'The PWA plugin did not emit a service worker. If that is deliberate,\n' +
      'delete this check; if it is not, the app just lost offline support.',
  )
}

const sw = readFileSync(swPath, 'utf8')

// 1. No client takeover, in either spelling workbox might emit.
for (const banned of ['clientsClaim', 'clients.claim']) {
  if (sw.includes(banned)) {
    fail(
      `dist/sw.js contains \`${banned}\`.\n\n` +
        'That lets a newly activated worker seize a page which was already served\n' +
        'the previous shell — the page then requests asset hashes that this deploy\n' +
        'rehashed, they 404, and it paints white.\n\n' +
        "Most likely cause: `registerType` is back to 'autoUpdate' in vite.config.ts,\n" +
        'which overrides the workbox flags. It must stay >prompt<.',
    )
  }
}

// 2. skipWaiting only on explicit request.
//
// Minified, the guarded form is roughly:
//   self.addEventListener("message", e => e.data && "SKIP_WAITING" === e.data.type && self.skipWaiting())
// so we count calls and require every one to sit near a SKIP_WAITING literal
// rather than trying to parse minified output.
const calls = sw.match(/skipWaiting\s*\(\s*\)/g) ?? []
const guarded = sw.match(/SKIP_WAITING[\s\S]{0,120}?skipWaiting\s*\(\s*\)/g) ?? []

if (calls.length !== guarded.length) {
  fail(
    `dist/sw.js calls skipWaiting() ${calls.length} time(s) but only ${guarded.length}\n` +
      'of those are behind the SKIP_WAITING message.\n\n' +
      'An unguarded skipWaiting() activates the new worker mid-load, underneath a\n' +
      'page that is still fetching the old bundle. Activation must happen only when\n' +
      'the page asks for it — src/pwa/registerPwa.ts asks only while hidden.',
  )
}

console.log(
  `✔ service worker shape: no client takeover, ${calls.length} guarded skipWaiting() call(s)`,
)
