// @vitest-environment jsdom
import { describe, it, expect, vi } from 'vitest'

// A lazy section's element carries no component name, only its loader. Record
// the loader's text so pageFile() can read the file it imports.
vi.mock('@/pwa/staleChunk', async (orig) => {
  const real = await orig<typeof import('@/pwa/staleChunk')>()
  return {
    ...real,
    lazyRoute: (load: () => Promise<unknown>) =>
      Object.assign(real.lazyRoute(load as never), { loadSource: String(load) }),
  }
})

import type { RouteObject } from 'react-router-dom'
import { routeTable } from '../router'
import { SURFACES, type SurfaceDescriptor } from './surfaces'
import { normalise, pageFile, pageText, type Sources } from './pageSource'

/**
 * Every UI label a guide descriptor quotes must appear in the source of the
 * page it describes.
 *
 * THE CONVENTION (surfaces.ts): a literal on-screen label or control name is
 * written in double quotes — “Keep a copy in canopy” in curly quotes, or
 * \"update missing\" in straight ones — inside `title`, `audience`, `what`,
 * `needsFirst` or an `actions` entry. Unquoted words are description, not a
 * promise about the page's text, and are not checked. So when you name a button
 * or a heading in a descriptor, quote it, and this test makes the page prove it.
 *
 * The page's source is its route's component file plus every src/ file that
 * file imports (pageSource.ts). Matching ignores case, curly-vs-straight
 * quotes, JSX entities and line wrapping.
 *
 * A quote that is legitimately not a literal in this page's source goes in
 * ALLOWED below, with the reason. Give `on` (a route path) when the label lives
 * on a different page the descriptor points to: it is then checked there. An
 * entry that no descriptor quotes any more fails, so the list cannot rot.
 */
const ALLOWED: Array<{ path: string; quote: string; reason: string; on?: string }> = [
  {
    path: '/w/:workspace/settings/roles',
    quote: 'read + use the Connect site',
    reason: 'an example of a role name an admin might define; it is data a workspace types in, not a label',
  },
  {
    path: '/w/:workspace/mail',
    quote: 'Keep a copy in canopy',
    on: '/w/:workspace/settings/email',
    reason: 'the checkbox that turns this inbox on is on the Email settings page, which the descriptor names',
  },
]

const raw = import.meta.glob('/src/**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }) as Sources
const sources: Sources = Object.fromEntries(
  Object.entries(raw).filter(([f]) => !/\.test\.tsx?$/.test(f) && !f.endsWith('.d.ts')),
)

/** The quoted labels in one string: “curly” or "straight" double quotes. */
export function quotedLabels(text: string): string[] {
  const out: string[] = []
  for (const m of text.matchAll(/“([^”]+)”|"([^"]+)"/g)) out.push((m[1] ?? m[2]).trim())
  return out
}

function stringsOf(s: SurfaceDescriptor): string[] {
  return [s.title, s.audience, s.what, s.needsFirst ?? '', ...(s.actions ?? [])]
}

function routesByPath(routes: RouteObject[], parent = '', out = new Map<string, RouteObject>()) {
  for (const r of routes) {
    const rawPath = r.path
    const here =
      rawPath === undefined ? parent : rawPath.startsWith('/') ? rawPath : `${parent.replace(/\/$/, '')}/${rawPath}`
    if (rawPath !== undefined) out.set(here, r)
    if (r.children?.length) routesByPath(r.children, here, out)
  }
  return out
}

const routes = routesByPath(routeTable)

describe('quotedLabels', () => {
  it('reads curly and straight double quotes, and nothing else', () => {
    expect(quotedLabels('Turn “Agents can learn from me” on; it says "update missing". Someone’s page, it\'s fine.')).toEqual([
      'Agents can learn from me',
      'update missing',
    ])
  })
})

describe('guide descriptors quote only labels their page contains', () => {
  const quoted = SURFACES.flatMap((s) => stringsOf(s).flatMap((t) => quotedLabels(t).map((q) => ({ s, q }))))

  it('finds the quotes and the pages (a floor, so the check cannot pass by finding nothing)', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(300)
    expect(quoted.length).toBeGreaterThanOrEqual(5)
    const placed = SURFACES.filter((s) => routes.has(s.path) && pageFile(routes.get(s.path)!, sources))
    expect(placed.length).toBeGreaterThan(SURFACES.length * 0.8)
  })

  it('finds every quoted label in its page source', () => {
    const missing: string[] = []
    for (const { s, q } of quoted) {
      const allowed = ALLOWED.find((d) => d.path === s.path && d.quote === q)
      if (allowed && !allowed.on) continue
      const route = routes.get(allowed?.on ?? s.path)
      const file = route ? pageFile(route, sources) : null
      if (!file) {
        missing.push(`${s.path}: quotes “${q}” but its page file cannot be found; allow it in ALLOWED with a reason`)
      } else if (!pageText(file, sources).includes(normalise(q))) {
        missing.push(`${allowed?.on ?? s.path}: “${q}” (quoted by ${s.path}) is not in ${file} or what it imports`)
      }
    }
    expect(missing, `descriptors quoting a label their page does not contain:\n${missing.join('\n')}`).toEqual([])
  })

  it('has no allowlist entry that no descriptor quotes any more', () => {
    const stale = ALLOWED.filter((d) => !quoted.some(({ s, q }) => s.path === d.path && q === d.quote))
    expect(stale.map((d) => `${d.path}: ${d.quote}`)).toEqual([])
    for (const d of ALLOWED) expect(d.reason.length, `${d.path} needs a reason`).toBeGreaterThan(10)
  })
})
