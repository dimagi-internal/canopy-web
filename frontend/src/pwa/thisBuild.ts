/**
 * Is the build the server now serves the one this page is running?
 *
 * A waiting service worker is not the same thing as a stale page. Navigations
 * are network-first, so a page loaded after a deploy is already current even
 * while the SW that a long-open window installed for that deploy is still
 * waiting — and offering "a new version" there sends someone to reload into
 * the page they already have.
 *
 * Every build's entry script carries a content hash in its name, and the
 * generated sw.js lists every precached asset by that name. So the server's
 * sw.js names this page's entry exactly when it is this page's build (a
 * backend-only deploy keeps the hash, and rightly reads as current).
 */

/** This page's entry script name (`index-<hash>.js`), or null outside a built app. */
export function thisPageEntry(doc: Document = document): string | null {
  const src = doc.querySelector('script[type="module"][src*="/assets/index-"]')?.getAttribute('src')
  return src?.split('?')[0].split('/').pop() || null
}

/** True when `swSource` (the server's sw.js) precaches this page's entry. Unknown → false,
 *  so the caller falls back to offering the reload. */
export function swIsThisBuild(swSource: string, entry: string | null): boolean {
  return !!entry && swSource.includes(`assets/${entry}"`)
}

export const __proof = 1
