import type { RouteObject } from 'react-router-dom'

/**
 * Finds the source text a route's page is made of, for guide/labels.test.tsx.
 *
 * A route's element names its component (or, for a lazy section, carries the
 * import() that loads it). The page's text lives in that file and in the
 * components it imports, so the "source of the page" is that file plus every
 * src/ file it imports, transitively — minus the shared plumbing in
 * `SHARED_DIRS`, whose strings belong to no one page.
 */
export type Sources = Record<string, string>

/** Imported by nearly everything; following them would make every page "contain" every label. */
const SHARED_DIRS = ['/src/api/', '/src/lib/', '/src/guide/', '/src/auth/', '/src/workspace/', '/src/components/ui/', '/src/components/AppLayout/']

const IMPORT_RE = /(?:from\s+|import\s*\(\s*)['"]([^'"]+)['"]/g

function resolveImport(from: string, spec: string, sources: Sources): string | null {
  let base: string
  if (spec.startsWith('@/')) base = '/src/' + spec.slice(2)
  else if (spec.startsWith('.')) {
    const parts = from.split('/').slice(0, -1)
    for (const seg of spec.split('/')) {
      if (seg === '..') parts.pop()
      else if (seg !== '.') parts.push(seg)
    }
    base = parts.join('/')
  } else return null
  for (const cand of [base, `${base}.tsx`, `${base}.ts`, `${base}/index.tsx`, `${base}/index.ts`]) {
    if (cand in sources) return cand
  }
  return null
}

/** `file` plus the src files it imports, transitively, in one normalised string. */
export function pageText(file: string, sources: Sources): string {
  const seen = new Set<string>()
  const queue = [file]
  while (queue.length) {
    const f = queue.pop()!
    if (seen.has(f)) continue
    seen.add(f)
    for (const m of sources[f].matchAll(IMPORT_RE)) {
      const dep = resolveImport(f, m[1], sources)
      if (dep && !seen.has(dep) && !SHARED_DIRS.some((d) => dep.startsWith(d))) queue.push(dep)
    }
  }
  return normalise([...seen].map((f) => sources[f]).join('\n'))
}

/** Case, curly quotes, JSX entities and line-wrapped whitespace all differ between a label as written and as seen. */
export function normalise(s: string): string {
  return s
    .replace(/&apos;|&#39;|[’‘]/g, "'")
    .replace(/&quot;|[“”]/g, '"')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/\\(['"])/g, '$1')
    .replace(/\s+/g, ' ')
    .toLowerCase()
}

type Typed = { type?: unknown; props?: { children?: unknown } }

function nameOrLoader(el: Typed | undefined): { name?: string; loader?: string } {
  const t = el?.type as
    | { name?: string; displayName?: string; _payload?: { _result?: { loadSource?: string } } }
    | string
    | undefined
  if (!t || typeof t === 'string') return {}
  const loader = t._payload?._result?.loadSource
  if (loader) return { loader }
  return { name: t.name || t.displayName }
}

const WRAPPERS = new Set(['LazySection', 'Suspense', 'RouteGuard'])

/**
 * The src file a route's page is in, or null when the element is a redirect or
 * a component this cannot place. `loadSource` is the text of a lazy section's
 * loader, recorded by labels.test.tsx's mock of lazyRoute.
 */
export function pageFile(route: RouteObject, sources: Sources): string | null {
  let el = route.element as Typed | undefined
  for (let i = 0; i < 4 && el; i++) {
    const { name, loader } = nameOrLoader(el)
    if (loader) {
      const m = loader.match(/["'](\/src\/[^"']+\.tsx?)["']/)
      return m && m[1] in sources ? m[1] : null
    }
    if (name && WRAPPERS.has(name)) {
      el = el.props?.children as Typed | undefined
      continue
    }
    if (!name) return null
    const decl = new RegExp(`export (?:default )?(?:function|const|class) ${name}\\b`)
    const hit = Object.keys(sources).find((f) => decl.test(sources[f]))
    return hit ?? null
  }
  return null
}
