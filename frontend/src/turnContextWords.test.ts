// The words a person reads say read mode, host site and profile, never door, ask or capability.
//
// `door` and `ask` (as the name of read mode) were a second vocabulary for the same things, and the
// agent sense of `capability` is now a host site, read mode or a profile
// (docs/superpowers/specs/2026-11-05-turn-context-design.md § 2, § 9). The server's half, and the
// API's field names, are `tests/test_turn_context_words.py`.
//
// Guarded: every piece of text the frontend can show — JSX text, a string given to a JSX attribute
// (labels, titles, placeholders), and any other string literal that is prose (holds a space). An
// identifier, a comment or a one-word key (`'capability:read'`, a wire value) is not text.
//
// What may still say them: leased machine capabilities (`MACHINE_CAPABILITY_FILES`, and the phrases in
// `MACHINE_PHRASES` wherever they appear), a different concept that keeps its name.
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const SOURCES = import.meta.glob('./**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }) as Record<string, string>

const DOOR = /\bdoors?\b/gi
// `ask` as a NAME: read mode's old name, a published entry, an agent's `:ask` command. Not the verb.
const ASK = /\b(?:use|capability|sensitive):ask\b|['"]ask['"]|\/[\w-]+:ask\b|\bask (?:capability|capabilities|profile|door|mode|entry|line)\b|\b(?:published|stamped|own|confined|default) (?:`ask`|ask\b)(?! (?:for|to|about)\b)/gi
const CAPABILITY = /\bcapabilit(?:y|ies)\b/gi
const MACHINE_PHRASES = /\b(?:machine|cloud|leased) capabilit(?:y|ies)\b|\bcapability (?:grants?|leases?|sweep)\b|publish-capabilities/gi

/** The cloud (machine) capabilities' own screens: another concept, which keeps its name. */
const MACHINE_CAPABILITY_FILES = new Set([
  'api/capabilities.ts',
  'pages/agents/AgentCapabilitiesSection.tsx',
  'pages/agents/CapabilityParts.tsx',
])

export function hits(text: string, { capability = true } = {}): string[] {
  const t = text.replace(MACHINE_PHRASES, '')
  const found = [...t.matchAll(DOOR), ...t.matchAll(ASK)].map((m) => m[0])
  if (capability) found.push(...[...t.matchAll(CAPABILITY)].map((m) => m[0]))
  return found
}

/** The modules a person's screen is built from: not tests, fixtures or generated types. */
function shownSources(): [string, string][] {
  return Object.entries(SOURCES)
    .map(([p, code]): [string, string] => [p.replace(/^\.\//, ''), code])
    .filter(([p]) => !/\.test\.tsx?$/.test(p) && !p.includes('__fixtures__') && !p.endsWith('generated.ts') && !p.endsWith('.d.ts'))
}

/** Every piece of text in one file a person can read. */
export function shownText(file: string, code: string): { line: number; text: string }[] {
  const src = ts.createSourceFile(file, code, ts.ScriptTarget.Latest, true, file.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
  const out: { line: number; text: string }[] = []
  const visit = (n: ts.Node) => {
    let text: string | null = null
    if (n.kind === ts.SyntaxKind.JsxText) text = (n as ts.JsxText).text
    else if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n) || ts.isTemplateHead(n) || ts.isTemplateMiddle(n) || ts.isTemplateTail(n)) {
      const value = (n as ts.LiteralLikeNode).text
      if ((n.parent && ts.isJsxAttribute(n.parent)) || /\s/.test(value.trim())) text = value
    }
    if (text && text.trim()) out.push({ line: src.getLineAndCharacterOfPosition(n.getStart()).line + 1, text })
    ts.forEachChild(n, visit)
  }
  visit(src)
  return out
}

describe('the words a person reads', () => {
  it('say read mode, host site and profile, never door, ask or capability', () => {
    const bad: string[] = []
    const files = shownSources()
    expect(files.length).toBeGreaterThan(500)
    for (const [rel, code] of files) {
      for (const { line, text } of shownText(rel, code)) {
        const found = hits(text, { capability: !MACHINE_CAPABILITY_FILES.has(rel) })
        if (found.length) bad.push(`${rel}:${line}: ${JSON.stringify(found)} in ${JSON.stringify(text.trim().slice(0, 90))}`)
      }
    }
    expect(bad, 'say read mode, host site or profile (spec § 2, § 9)').toEqual([])
  })

  it('catches each word, and leaves the verb, wire values and machine capabilities alone', () => {
    expect(hits('includes read mode and every door')).toEqual(['door'])
    expect(hits('names of capabilities an agent published')).toEqual(['capabilities'])
    expect(hits("confined to the caller capability 'ask'")).toEqual(["'ask'", 'capability'])
    expect(hits('About it: started by /hal:ask')).toEqual(['/hal:ask'])
    expect(hits('Ask Hal a question')).toEqual([])
    expect(hits('Grant a cloud capability (admins)')).toEqual([])
    expect(hits('Capabilities', { capability: false })).toEqual([])
    const shown = shownText('x.tsx', `const k = 'capability:ask'\n// a door in a comment\nexport const A = () => <p title="Doors">Read mode and doors</p>`)
    expect(shown.map((s) => s.text)).toEqual(['Doors', 'Read mode and doors'])
  })
})
