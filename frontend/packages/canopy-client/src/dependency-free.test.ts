import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

/**
 * The package's reason for existing is that it depends on nothing.
 *
 * canopy-ui requires React 19; connect-labs is React 18, and mostly Django
 * templates with alpine and htmx besides. If this package ever grows an import
 * of a framework — or of anything at all — the hosts it was built for can no
 * longer use it, and the failure would show up as a peer-dependency conflict
 * in someone else's repo rather than as a test here.
 */

const SRC = dirname(fileURLToPath(import.meta.url))

function shippedFiles(): string[] {
  return readdirSync(SRC).filter((f) => f.endsWith('.ts') && !f.endsWith('.test.ts'))
}

describe('the package ships with no dependencies', () => {
  it('declares none in package.json', () => {
    const pkg = JSON.parse(readFileSync(join(SRC, '..', 'package.json'), 'utf8'))
    expect(pkg.dependencies).toBeUndefined()
    expect(pkg.peerDependencies).toBeUndefined()
    expect(pkg.devDependencies).toBeUndefined()
  })

  it('imports nothing but its own modules', () => {
    // Matches `from "x"` / `from 'x'` where x is not relative.
    const bare = /from\s+['"]([^.'"][^'"]*)['"]/g
    const offenders: string[] = []

    for (const file of shippedFiles()) {
      const source = readFileSync(join(SRC, file), 'utf8')
      for (const [, spec] of source.matchAll(bare)) {
        offenders.push(`${file} imports ${spec}`)
      }
    }

    expect(offenders).toEqual([])
  })

  it('covers every shipped module, so a new file cannot slip past the check', () => {
    expect(shippedFiles().sort()).toEqual(['bridge.ts', 'index.ts', 'rest.ts', 'token.ts', 'ws.ts'])
  })
})
