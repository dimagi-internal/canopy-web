import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

/**
 * The loader must stay dependency-free, for a stricter reason than the client.
 *
 * This code runs on SOMEBODY ELSE'S PAGE. Anything it imports is shipped into a
 * host we do not control, where it can collide with whatever that page already
 * loads — connect-labs alone carries React 18, alpine, htmx and a dozen Radix
 * packages. The chat UI's React lives inside the iframe precisely so it cannot
 * meet any of that; a dependency added here would undo the isolation the iframe
 * exists to provide.
 */

const SRC = dirname(fileURLToPath(import.meta.url))

function shippedFiles(): string[] {
  return readdirSync(SRC).filter((f) => f.endsWith('.ts') && !f.endsWith('.test.ts'))
}

describe('the loader ships with no dependencies', () => {
  it('declares none in package.json', () => {
    const pkg = JSON.parse(readFileSync(join(SRC, '..', 'package.json'), 'utf8'))
    expect(pkg.dependencies).toBeUndefined()
    expect(pkg.peerDependencies).toBeUndefined()
    expect(pkg.devDependencies).toBeUndefined()
  })

  it('imports nothing but its own modules', () => {
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
    expect(shippedFiles().sort()).toEqual(['chrome.ts', 'index.ts', 'protocol.ts'])
  })

  it('never posts to a wildcard target origin', () => {
    // The token crosses this boundary. A '*' targetOrigin would hand it to
    // whatever is in the frame if its src were ever redirected — so the literal
    // must not appear in a postMessage call at all.
    for (const file of shippedFiles()) {
      const source = readFileSync(join(SRC, file), 'utf8')
      expect(source).not.toMatch(/postMessage\([^)]*['"]\*['"]/)
    }
  })
})
