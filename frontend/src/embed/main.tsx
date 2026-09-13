import { createRoot } from 'react-dom/client'

import '../index.css'
import { EmbedApp } from './EmbedApp'
import { createHostLink, type EmbedBootstrap } from './hostLink'

/**
 * Entry point for the widget's iframe.
 *
 * Deliberately NOT the app's entry: no router, no AuthProvider, no service
 * worker, no PWA registration. A widget that registered canopy's service
 * worker inside a third-party page would drag the whole SPA in with it — the
 * shape of issue #345, where an iframe rendered the entire app inside itself.
 *
 * `window.CANOPY_EMBED` is injected by the server that rendered the shell
 * (apps/tokens/views_embed.py), which is the only party that can be trusted to
 * say which origins may talk to this frame — the host is the one being
 * authenticated, so a list it supplied would authenticate nothing.
 */

declare global {
  interface Window {
    CANOPY_EMBED?: EmbedBootstrap
  }
}

const bootstrap = window.CANOPY_EMBED
const mount = document.getElementById('canopy-widget-boot')

if (!mount) {
  // The shell always provides it; if it is missing, the shell and this bundle
  // have drifted and silence would be the worst outcome.
  console.error('canopy embed: no mount point in the shell')
} else if (!bootstrap?.app || !Array.isArray(bootstrap.origins)) {
  // Fail visibly rather than open a frame that will accept messages from
  // anywhere: with no origin list there is nothing to validate against.
  mount.textContent = 'This widget is not configured correctly.'
} else {
  const link = createHostLink(bootstrap)
  createRoot(mount).render(<EmbedApp link={link} app={bootstrap.app} />)
}
