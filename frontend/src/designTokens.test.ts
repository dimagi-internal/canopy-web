import { describe, expect, it } from 'vitest'

/**
 * Design tokens are the single source of truth (CLAUDE.md, docs/architecture/frontend.md):
 * components style off semantic tokens (`bg-card`, `text-muted-foreground`, `text-warning`…),
 * never raw Tailwind palette literals (`stone-*`, `amber-*`, …) and never `dark:` variants —
 * the per-theme values live in `index.css`, which is what makes both themes work.
 *
 * This scans every source file in `src/` and `packages/canopy-ui/src/` and fails on either.
 * A colour a token does not yet cover gets a token in `index.css`, not a literal here.
 */

// Every Tailwind colour family. A utility prefix (`bg-`, `text-`, `ring-`, `from-`…) then
// the family then a shade: `bg-amber-500`, `text-zinc-900/50`, `border-l-sky-500`.
const FAMILIES =
  'slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose'
const PALETTE_LITERAL = new RegExp(`[a-z]-(?:${FAMILIES})-(?:50|[1-9]00|950)\\b`)
// A `dark:` variant is `dark:` directly followed by a utility — prose like "dark: there"
// in a comment is not one.
const DARK_VARIANT = /(?:^|[\s"'`:])dark:[a-z[!-]/

// The documented exceptions — keep in lockstep with docs/architecture/frontend.md
// § Design tokens. Paths are relative to `frontend/`.
const ALLOWED: Record<string, string> = {
  // The public share viewer: a deliberately light-themed page outside the app shell.
  'src/pages/SessionSharePage.tsx': 'public share viewer (light-only, outside the app shell)',
  'src/components/transcript/': 'public share viewer (light-only, outside the app shell)',
  'src/components/ShareRouteErrorBoundary.tsx': "the share viewer's own error boundary",
  // canopy-ui is a published package (changing its src needs a version bump). These carry
  // stock shadcn `dark:` variants or a categorical per-person hue set no token models.
  'packages/canopy-ui/src/ui/badge.tsx': 'stock shadcn dark: variants',
  'packages/canopy-ui/src/ui/button.tsx': 'stock shadcn dark: variants',
  'packages/canopy-ui/src/ui/input.tsx': 'stock shadcn dark: variants',
  'packages/canopy-ui/src/ui/textarea.tsx': 'stock shadcn dark: variants',
  'packages/canopy-ui/src/chat/personColor.ts': 'categorical per-person hues',
  'packages/canopy-ui/src/presence/avatar.ts': 'categorical per-person hues',
  'packages/canopy-ui/src/presence/avatar.test.ts': 'asserts those hues',
}

const raw = {
  ...import.meta.glob('./**/*.{ts,tsx,css}', { query: '?raw', import: 'default', eager: true }),
  ...import.meta.glob('../packages/canopy-ui/src/**/*.{ts,tsx,css}', {
    query: '?raw',
    import: 'default',
    eager: true,
  }),
} as Record<string, string>

// './pages/X.tsx' -> 'src/pages/X.tsx'; '../packages/…' -> 'packages/…'
const sources = Object.entries(raw).map(
  ([key, text]) => [key.startsWith('../') ? key.slice(3) : `src/${key.slice(2)}`, text] as const,
)

const THIS_FILE = 'src/designTokens.test.ts'

function allowed(path: string): boolean {
  return Object.keys(ALLOWED).some((p) => (p.endsWith('/') ? path.startsWith(p) : path === p))
}

function violations(pattern: RegExp): string[] {
  const out: string[] = []
  for (const [path, text] of sources) {
    if (path === THIS_FILE || allowed(path)) continue
    text.split('\n').forEach((line, i) => {
      const m = line.match(pattern)
      if (m) out.push(`${path}:${i + 1}  ${m[0].trim()}`)
    })
  }
  return out
}

describe('design tokens', () => {
  it('scans both source roots', () => {
    expect(sources.some(([p]) => p.startsWith('src/pages/'))).toBe(true)
    expect(sources.some(([p]) => p.startsWith('packages/canopy-ui/src/'))).toBe(true)
  })

  it('uses no raw Tailwind palette literals outside the documented exceptions', () => {
    expect(violations(PALETTE_LITERAL)).toEqual([])
  })

  it('uses no dark: variants outside the documented exceptions', () => {
    expect(violations(DARK_VARIANT)).toEqual([])
  })

  it('has no stale allowlist entries', () => {
    const stale = Object.keys(ALLOWED).filter(
      (p) => !sources.some(([path]) => (p.endsWith('/') ? path.startsWith(p) : path === p)),
    )
    expect(stale).toEqual([])
  })

  it('catches what it claims to', () => {
    expect(PALETTE_LITERAL.test('className="bg-amber-500/15"')).toBe(true)
    expect(PALETTE_LITERAL.test('border-l-sky-500')).toBe(true)
    expect(PALETTE_LITERAL.test('bg-warning/15 text-muted-foreground')).toBe(false)
    expect(DARK_VARIANT.test('"text-sm dark:text-amber-400"')).toBe(true)
    expect(DARK_VARIANT.test('placeholder:x dark:placeholder:text-foreground')).toBe(true)
    expect(DARK_VARIANT.test('// treated as dark: there is no age')).toBe(false)
  })
})
