import { describe, expect, it } from 'vitest'

/**
 * Tenant links are built with `scopedPath` / `useScopedPath` (`lib/scopedLinks.ts`),
 * never by hand (CLAUDE.md § Key URLs). The helper is the one place that encodes the
 * workspace and refuses to address a page under no workspace; a hand-built
 * `` `/w/${ws}/…` `` does neither, which is how a walkthrough list came to link
 * `/w/<walkthrough id>` — a workspace that does not exist.
 *
 * Tests are exempt: they assert the URLs the helper produces.
 */
const HELPER = './lib/scopedLinks.ts'

const sources = import.meta.glob('./**/*.{ts,tsx}', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

const HAND_BUILT = [/`\/w\/\$\{/, /['"]\/w\/['"]\s*\+/]

describe('tenant links go through scopedPath', () => {
  it('has no hand-built /w/ paths outside the helper', () => {
    const bad: string[] = []
    for (const [path, text] of Object.entries(sources)) {
      if (path === HELPER || /\.test\.tsx?$/.test(path)) continue
      text.split('\n').forEach((line, i) => {
        if (HAND_BUILT.some((re) => re.test(line))) bad.push(`${path}:${i + 1}  ${line.trim()}`)
      })
    }
    expect(bad).toEqual([])
  })

  it('catches what it claims to', () => {
    expect(HAND_BUILT.some((re) => re.test('to={`/w/${ws}/agents`}'))).toBe(true)
    expect(HAND_BUILT.some((re) => re.test("'/w/' + ws"))).toBe(true)
    expect(HAND_BUILT.some((re) => re.test("scopedPath(ws, '/agents')"))).toBe(false)
  })
})
