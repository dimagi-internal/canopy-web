import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { afterAll, beforeAll, describe, expect, it } from 'vitest'

/**
 * What npm actually ships, checked the way a host consumes it.
 *
 * 0.1.0 shipped TypeScript source. It passed every test here, because every
 * test imported `src/` — and it failed in connect-labs' webpack (babel with
 * `exclude: /node_modules/`), the host this package exists for, with
 * `Module parse failed: Unexpected token`. So these tests build the real
 * artifact and load it with plain Node, which compiles nothing: if Node can
 * import it, a bundler that leaves node_modules alone can too.
 */

const PKG_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '..')
let out = ''

beforeAll(() => {
  out = mkdtempSync(join(tmpdir(), 'canopy-client-dist-'))
  execFileSync(process.execPath, [join(PKG_DIR, 'scripts', 'build-dist.mjs'), out], { stdio: 'pipe' })
}, 60_000)

afterAll(() => rmSync(out, { recursive: true, force: true }))

describe('the published build', () => {
  it('contains JavaScript and declarations, and no TypeScript source', () => {
    const files = readdirSync(out)
    expect(files).toContain('index.js')
    expect(files).toContain('index.d.ts')
    expect(files.filter((f) => f.endsWith('.ts') && !f.endsWith('.d.ts'))).toEqual([])
    // Tests are not part of the package.
    expect(files.filter((f) => f.includes('.test.'))).toEqual([])
  })

  it('loads in plain Node — both entry points', () => {
    // A real `node` process, not vitest's module runner: the claim is "Node
    // compiles nothing and still loads this", and vitest's loader is neither
    // plain Node nor a faithful stand-in (it instantiated bridge.js twice,
    // which real Node does not).
    const probe = `
      const main = await import(${JSON.stringify(pathToFileURL(join(out, 'index.js')).href)})
      const bridge = await import(${JSON.stringify(pathToFileURL(join(out, 'bridge.js')).href)})
      console.log(JSON.stringify({
        client: typeof main.createCanopyClient,
        bridge: typeof bridge.createHostBridge,
        // One module graph, not two copies: an error the bridge throws must be
        // an instance of the class the main entry exports.
        sameErrorClass: main.UnknownActionError === bridge.UnknownActionError,
      }))
    `
    const result = JSON.parse(
      execFileSync(process.execPath, ['--input-type=module', '-e', probe], { encoding: 'utf8' }),
    )

    expect(result).toEqual({ client: 'function', bridge: 'function', sameErrorClass: true })
  })

  it('has a manifest whose every export points at a file that exists', () => {
    const pkg = JSON.parse(readFileSync(join(out, 'package.json'), 'utf8'))

    expect(pkg.name).toBe('canopy-client')
    for (const [subpath, target] of Object.entries(pkg.exports as Record<string, unknown>)) {
      const targets = typeof target === 'string' ? [target] : Object.values(target as object)
      for (const t of targets as string[]) {
        expect(existsSync(join(out, t)), `${subpath} -> ${t}`).toBe(true)
      }
    }
  })

  it('still declares no dependencies of any kind', () => {
    const pkg = JSON.parse(readFileSync(join(out, 'package.json'), 'utf8'))
    expect(pkg.dependencies).toBeUndefined()
    expect(pkg.peerDependencies).toBeUndefined()
  })

  it('ships its README, so the npm page says what the package is', () => {
    expect(existsSync(join(out, 'README.md'))).toBe(true)
  })
})
