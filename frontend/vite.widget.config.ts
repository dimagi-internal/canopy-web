import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { defineConfig, type Plugin } from 'vite'

/**
 * Fail the BUILD if the emitted loader would not give a host `canopy.init(...)`.
 *
 * Same reasoning as the `sw.js` guard in vite.config.ts: every property below
 * is a silent default that a vite or rollup bump could restore, and none of
 * them shows up as a type error or a source test — the source is identical
 * either way, only the artifact changes. The failure mode is a host's script
 * tag loading a file that defines nothing, or that needs `canopy.default.init`,
 * and neither is discoverable except by embedding it somewhere.
 */
function assertLoaderShape(outFile: string): Plugin {
  return {
    name: 'canopy-widget-shape-guard',
    closeBundle() {
      const code = readFileSync(outFile, 'utf8')
      if (!/\bvar canopy\s*=/.test(code)) {
        throw new Error(
          `canopy widget: ${outFile} does not define the \`canopy\` global. ` +
            'A host loads this with a plain <script> tag, so an IIFE assigning ' +
            'that global IS the API — check build.lib.formats and build.lib.name.',
        )
      }
      // Matches the shape rollup actually emits: a property assignment on the
      // minified namespace object (`e.default={init:s}`) — NOT `exports.default =`
      // and not a `default:` key. An earlier version of this guard checked for
      // those two and passed a build that really did require `canopy.default.init`.
      if (/\.default\s*=/.test(code)) {
        throw new Error(
          `canopy widget: ${outFile} emits a default export, which makes the ` +
            'documented `canopy.init(...)` reachable only as `canopy.default.init`. ' +
            "Set rollupOptions.output.exports to 'named'.",
        )
      }
      if (/\bimport\s*\(/.test(code) || /\brequire\s*\(/.test(code)) {
        throw new Error(
          `canopy widget: ${outFile} contains a dynamic import or require. The ` +
            'loader must be one self-contained file — a host page has no module ' +
            'loader and no way to fetch a sibling chunk.',
        )
      }
    },
  }
}

/**
 * The embeddable widget loader, built as one self-contained script.
 *
 * Separate from the app build on purpose. This artifact is loaded by a
 * `<script>` tag on a page we do not control, so it has requirements the SPA
 * bundle does not:
 *
 *  - **IIFE, not ESM.** A host may have no module loader at all. The supply app
 *    in connect-labs is non-module scripts sharing one global scope with a
 *    global React — `type="module"` is not available there.
 *  - **One global, no imports.** `window.canopy` is the entire API surface.
 *  - **A FIXED filename.** Hosts hardcode `/embed/widget.js` in their HTML, so
 *    it cannot be content-hashed. The cache policy already handles that
 *    correctly: unhashed ⇒ `no-cache` (config/static_cache.py), so a host
 *    revalidates and picks up a new loader without changing their tag.
 *  - **No CSS.** The loader's chrome is inlined into a shadow root
 *    (packages/canopy-widget/src/chrome.ts); a stylesheet would be a second
 *    request a host would have to add, and a global one could leak onto their
 *    page.
 *
 * The chat UI is deliberately NOT in here — it lives in the iframe with its own
 * React, which is what keeps this bundle tiny and framework-collision-proof.
 */
const OUT_FILE = resolve(__dirname, 'dist/embed/widget.js')

export default defineConfig({
  plugins: [assertLoaderShape(OUT_FILE)],
  // The app's `public/` (icons, favicon, sw-push.js) is not the widget's, and
  // vite would otherwise copy all of it into this outDir — shipping a second
  // copy of every PWA icon next to a 6 kB script.
  publicDir: false,
  build: {
    // Alongside the app build rather than replacing it: `emptyOutDir: false` so
    // running one does not delete the other's output.
    outDir: resolve(__dirname, 'dist/embed'),
    emptyOutDir: false,
    lib: {
      entry: resolve(__dirname, 'packages/canopy-widget/src/index.ts'),
      formats: ['iife'],
      name: 'canopy',
      fileName: () => 'widget.js',
    },
    // A host page is not our build: modern-but-not-bleeding-edge, so the loader
    // works in whatever the host's users already have.
    target: 'es2019',
    sourcemap: true,
    rollupOptions: {
      // Nothing external — the whole point is that a host needs to install
      // nothing. The package's own guard test asserts it imports nothing, so
      // there is nothing here to externalise.
      external: [],
      // `canopy.init(...)` is the documented API. Without this, rollup sees the
      // module's named AND default exports and warns that consumers must reach
      // through `canopy.default` — which would make every published example
      // wrong.
      output: { exports: 'named' },
    },
  },
})
