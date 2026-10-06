// canopy.dimagi.com — the public product site.
//
// Static output only. Built files land in dist/, and every asset goes under
// /site/assets/ as <name>-<hash>.<ext>: under /site/ so nothing collides with the
// SPA's /assets/ while canopy-web serves both from one host (config/public_site.py),
// and in that naming because config/static_cache.py marks exactly that shape
// immutable. Moving the site to its own host later needs neither rule.
import { defineConfig } from 'astro/config'

export default defineConfig({
  output: 'static',
  trailingSlash: 'never',
  build: { format: 'directory', assets: 'site/assets' },
  vite: {
    build: {
      rollupOptions: {
        output: {
          assetFileNames: 'site/assets/[name]-[hash][extname]',
          chunkFileNames: 'site/assets/[name]-[hash].js',
          entryFileNames: 'site/assets/[name]-[hash].js',
        },
      },
    },
  },
})
