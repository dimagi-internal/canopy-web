/**
 * The `sandbox` for every iframe that shows UPLOADED content (a walkthrough deck,
 * a DDD docs page, a review cut) — bytes an editor or an agent wrote, served from
 * canopy's own origin.
 *
 * Deliberately NO `allow-same-origin`. With it, `allow-scripts` on a same-origin
 * URL is no sandbox at all: a script in the deck runs as the viewer and can call
 * the cookie-authenticated API (mint a PAT, read every chat). Without it the frame
 * gets an opaque origin, so the deck's own navigation script (arrow keys,
 * #scene-N, the theme toggle) still runs and nothing else is reachable.
 *
 * The server says the same thing independently — `/walkthrough/<id>/content`
 * carries `Content-Security-Policy: sandbox …` with this exact token list
 * (apps/walkthroughs/streaming.py::SANDBOX_CSP) — so opening the content URL
 * directly, outside any frame, is contained too. Nothing in the app reads these
 * frames' `contentDocument`/`contentWindow`; keep it that way.
 */
export const UPLOADED_CONTENT_SANDBOX =
  'allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads'
