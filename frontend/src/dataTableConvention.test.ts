import { describe, expect, it } from 'vitest'
import { PAD_CLASS } from './components/DataTable'

/**
 * One table convention (docs/architecture/frontend.md § Tables): a header cell is `<Th>`, a body
 * cell `<Td>`, a row `<Tr>` from `components/DataTable`, not a hand-written
 * `<th className="py-1 font-medium">` / `<td className="py-1.5 pr-3 text-muted-foreground">` /
 * `<tr className="border-b border-border last:border-0">`. Three checks:
 *
 * 1. no raw `<th>`/`<td>`/`<tr>` with a literal `className` carries something the primitives own
 *    (a standard density's padding, a tone, alignment, the row rule…), anywhere;
 * 2. no raw `<th>` at all (a header is always a `Th`), except the files in `NOT_YET_MIGRATED`;
 * 3. a raw `<td>`/`<tr>` whose `className` is computed (`cn(...)`, a template) is kept only where
 *    the class depends on the row, and only as many as `COMPUTED_CLASS` lists, so the count can
 *    only shrink: a new one fails, and a removed one must lower its entry.
 */

// Files still hand-writing `<th>` (paths relative to `frontend/`), and how many. Empty: every
// header cell is a `Th`.
const NOT_YET_MIGRATED: Record<string, number> = {}

// Raw `<td>`/`<tr>` whose className is computed from the row's data. Paths relative to `frontend/`.
const COMPUTED_CLASS: Record<string, number> = {}

const raw = import.meta.glob('./**/*.tsx', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
const sources = Object.entries(raw)
  .map(([key, text]) => [`src/${key.slice(2)}`, text] as const)
  .filter(([path]) => path !== 'src/components/DataTable.tsx' && !path.endsWith('.test.tsx'))

const RAW_TAG = /<(th|td|tr)(?=[\s>/])/g
// An opening tag with a string-literal className (single line or not).
const LITERAL = /<(th|td|tr)\s+(?:[^>]*?\s)?className="([^"]*)"/g
const COMPUTED = /<(td|tr)\s+(?:[^>]*?\s)?className=\{/g

// Classes the primitives own, per tag. A non-standard padding (`p-3`, `pl-3`) is not one of them.
const PADDINGS = Object.values(PAD_CLASS)
  .filter(Boolean)
  .map((s) => s.split(' '))
const CELL_OWNED = [
  'font-medium', 'font-normal', 'text-left', 'text-right', 'text-center', 'sr-only',
  'text-foreground', 'text-foreground-secondary', 'text-muted-foreground',
  'tabular-nums', 'whitespace-nowrap', 'font-mono',
]
const ROW_OWNED = ['align-top', 'text-left', 'border-border', 'border-border/50']

function owned(tag: string, classes: string): string[] {
  const have = classes.split(/\s+/).filter(Boolean)
  const found: string[] = []
  if (tag !== 'tr') {
    for (const pad of PADDINGS) if (pad.every((t) => have.includes(t))) found.push(pad.join(' '))
    found.push(...CELL_OWNED.filter((t) => have.includes(t)))
    if (have.includes('hidden') && have.some((t) => /^(sm|md|lg|xl):table-cell$/.test(t))) found.push('hidden …:table-cell')
  } else {
    found.push(...ROW_OWNED.filter((t) => have.includes(t)))
  }
  return found
}

describe('table convention', () => {
  it('scans the app sources', () => {
    expect(sources.some(([p]) => p.startsWith('src/pages/'))).toBe(true)
  })

  it('has no raw th/td/tr carrying a class the Th/Td/Tr primitives own', () => {
    const out: string[] = []
    for (const [path, text] of sources) {
      for (const m of text.matchAll(LITERAL)) {
        const found = owned(m[1], m[2])
        if (found.length) out.push(`${path}  <${m[1]} className="${m[2]}">  (${found.join(', ')})`)
      }
    }
    expect(out).toEqual([])
  })

  it('has no hand-written <th>, and the allowlist only shrinks', () => {
    const counts: Record<string, number> = {}
    for (const [path, text] of sources) {
      const n = [...text.matchAll(RAW_TAG)].filter((m) => m[1] === 'th').length
      if (n) counts[path] = n
    }
    const over = Object.entries(counts)
      .filter(([p, n]) => n > (NOT_YET_MIGRATED[p] ?? 0))
      .map(([p, n]) => `${p}: ${n} raw <th> (allowed ${NOT_YET_MIGRATED[p] ?? 0}) — use <Th> from components/DataTable`)
    expect(over).toEqual([])
    const stale = Object.entries(NOT_YET_MIGRATED)
      .filter(([p, n]) => (counts[p] ?? 0) < n)
      .map(([p, n]) => `${p}: lower its entry from ${n} to ${counts[p] ?? 0} (delete it at 0)`)
    expect(stale).toEqual([])
  })

  it('keeps raw td/tr with a computed className from growing, and the allowlist only shrinks', () => {
    const counts: Record<string, number> = {}
    for (const [path, text] of sources) {
      const n = [...text.matchAll(COMPUTED)].length
      if (n) counts[path] = n
    }
    const over = Object.entries(counts)
      .filter(([p, n]) => n > (COMPUTED_CLASS[p] ?? 0))
      .map(([p, n]) => `${p}: ${n} raw td/tr with a computed className (allowed ${COMPUTED_CLASS[p] ?? 0}) — use <Td>/<Tr> and pass the row-dependent part as className`)
    expect(over).toEqual([])
    const stale = Object.entries(COMPUTED_CLASS)
      .filter(([p, n]) => (counts[p] ?? 0) < n)
      .map(([p, n]) => `${p}: lower its entry from ${n} to ${counts[p] ?? 0} (delete it at 0)`)
    expect(stale).toEqual([])
  })

  it('catches what it claims to', () => {
    expect(owned('th', 'py-1 font-medium')).toEqual(['py-1', 'font-medium'])
    expect(owned('td', 'px-3 py-2 text-right')).toContain('px-3 py-2')
    expect(owned('td', 'py-1.5 pr-3 text-muted-foreground')).toContain('py-1.5 pr-3')
    expect(owned('td', 'bg-muted/30 p-3')).toEqual([])
    expect(owned('td', 'hidden md:table-cell')).toEqual(['hidden …:table-cell'])
    expect(owned('tr', 'border-b border-border align-top')).toEqual(['align-top', 'border-border'])
    expect(owned('tr', 'cursor-pointer hover:bg-muted/50')).toEqual([])
  })
})
