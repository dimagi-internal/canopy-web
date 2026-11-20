/**
 * Chrome's match-pattern rule for the https patterns a manifest's `browser` entry may
 * declare — the twin of `apps/workspace_apps/browser.py::matches`, so the panel asks the
 * same question of the tab before every call that the server asked when it listed the
 * page. The host exactly (or any subdomain, for `*.`), on the default port, no
 * credentials; the path plus query by glob; never the fragment.
 */
const PATTERN = /^https:\/\/((?:\*\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*)(\/[^\s#]*)$/
const MAX_URL = 2048

/** `*` matches any run of characters; everything else literally. No RegExp is built from a pattern. */
function globMatch(pattern: string, text: string): boolean {
  const parts = pattern.split('*')
  if (parts.length === 1) return pattern === text
  const first = parts[0]
  const last = parts[parts.length - 1]
  if (!text.startsWith(first) || text.length < first.length + last.length || !text.endsWith(last)) return false
  let at = first.length
  const end = text.length - last.length
  for (const middle of parts.slice(1, -1)) {
    const found = text.indexOf(middle, at)
    if (found < 0 || found + middle.length > end) return false
    at = found + middle.length
  }
  return true
}

export function matchesPattern(pattern: string, url: string): boolean {
  const m = PATTERN.exec(pattern ?? '')
  if (!m || typeof url !== 'string' || url.length > MAX_URL) return false
  let u: URL
  try {
    u = new URL(url)
  } catch {
    return false
  }
  if (u.protocol !== 'https:' || !u.hostname || (u.port !== '' && u.port !== '443') || u.username || u.password) return false
  const host = u.hostname.toLowerCase()
  const want = m[1]
  if (want.startsWith('*.')) {
    if (!(host === want.slice(2) || host.endsWith(want.slice(1)))) return false
  } else if (host !== want) return false
  return globMatch(m[2], (u.pathname || '/') + u.search)
}

export function matchesAny(patterns: readonly string[], url: string | null | undefined): boolean {
  return !!url && patterns.some((p) => matchesPattern(p, url))
}
