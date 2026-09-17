import { describe, expect, it } from 'vitest'
import pkg from '../package.json'

/**
 * Every in-repo workspace package that `src/` imports must be a DECLARED
 * dependency in frontend/package.json.
 *
 * WHY THIS EXISTS. `main` was undeployable for three hours with CI green the
 * whole time. #762 imported `canopy-client` — a local workspace under
 * `frontend/packages/` — without adding it to `dependencies`. That resolves
 * locally and in CI, because `npm install` in a workspace root links every
 * workspace into `node_modules` regardless of the declared graph. The Docker
 * build runs `npm ci`, which follows the lockfile's dependency graph strictly,
 * so the symlink was never created and `tsc -b` failed with "Cannot find
 * module 'canopy-client'".
 *
 * So the failure is invisible to `npm run build` AND to the CI job literally
 * named "Frontend build", and shows up only when an image is built — at deploy
 * time, on main, after the merge. This test closes that gap in the one place
 * that runs everywhere.
 *
 * It asserts the DECLARATION, not resolvability: whether a module resolves is
 * exactly what differs between environments, so asserting that here would pass
 * for the same reason the bug hid.
 *
 * `canopy-widget` is deliberately NOT required to be declared — nothing in
 * `src/` imports it; it is built from its own directory by `npm run
 * build:widget`, which needs the source on disk rather than a node_modules
 * symlink. Verified by a real `docker build`.
 */
describe('workspace package imports', () => {
  const declared = new Set(
    Object.keys((pkg as { dependencies: Record<string, string> }).dependencies),
  )

  // Package NAMES of the in-repo workspaces (package.json declares
  // `workspaces: ["packages/*"]`). These are what an import specifier carries.
  const WORKSPACE_NAMES = ['canopy-ui', 'canopy-client', 'canopy-widget']

  // Read every source file through Vite, so a new import anywhere in src/ is
  // covered without anyone remembering to add it here.
  const sources = import.meta.glob('./**/*.{ts,tsx}', {
    query: '?raw',
    import: 'default',
    eager: true,
  }) as Record<string, string>

  function importersOf(name: string): string[] {
    const patterns = [`from '${name}'`, `from "${name}"`, `from '${name}/`, `from "${name}/`]
    return Object.entries(sources)
      .filter(([path]) => !path.endsWith('workspaceDeps.test.ts'))
      .filter(([, src]) => patterns.some((p) => src.includes(p)))
      .map(([path]) => path)
  }

  it('reads the source tree at all', () => {
    // A glob that silently matched nothing would make every assertion below
    // vacuously pass — which is the failure mode this whole file exists to
    // prevent, so it gets its own check.
    expect(Object.keys(sources).length).toBeGreaterThan(100)
  })

  it.each(WORKSPACE_NAMES)('%s: if src/ imports it, it is declared', (name) => {
    const importers = importersOf(name)
    if (importers.length === 0) return
    expect(
      declared.has(name),
      `${name} is imported by ${importers.slice(0, 3).join(', ')} but is not in ` +
        'frontend/package.json dependencies. It resolves locally and in CI (npm install ' +
        'links all workspaces) and FAILS in the Docker build (npm ci follows the declared ' +
        'graph) — a green CI and an undeployable main.',
    ).toBe(true)
  })

  it('still sees the two imports that motivated this', () => {
    // Guards the detector itself: if the pattern matching broke, the checks
    // above would pass by finding nothing.
    expect(importersOf('canopy-ui').length).toBeGreaterThan(0)
    expect(importersOf('canopy-client').length).toBeGreaterThan(0)
  })
})
