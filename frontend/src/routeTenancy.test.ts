// @vitest-environment jsdom
/**
 * Nothing on canopy outside a workspace (owner decision, 2026-10-08;
 * canopy-web#1289).
 *
 * Every route that shows tenant data lives under `/w/:workspace`. A route
 * outside it is allowed only WITH ITS REASON — a page's `flat` in the registry
 * (guide/surfaces.ts), a redirect's entry in guide/coverage.ts — so adding a
 * flat page fails this test until someone says why it may be flat, instead of
 * a share link quietly opening from any workspace again (which is how the
 * chlorine narrative sat in `dimagi` for a week with nobody able to tell).
 *
 * The reasons sort into four kinds:
 *   - personal/global: about YOU, or the fleet across your workspaces;
 *   - pre-tenant: you have no workspace yet (an invite, the first-run form);
 *   - redirect: only sends the browser to a scoped page — never an ARTIFACT
 *     link, which has one address and is not forwarded (canopy-web#1337);
 *   - FOLLOW-UP: tenant data still on a flat public URL. There are none left
 *     (the last three moved in canopy-web#1337's follow-up); a new one fails
 *     the test below.
 */
import { describe, expect, it } from 'vitest'
import { routeTable } from './router'
import { NOT_PAGES, flattenRoutePaths } from './guide/coverage'
import { SURFACES } from './guide/surfaces'

// A page's reason is its registry `flat`; a redirect's is its NOT_PAGES entry.
const FLAT_ALLOWED: Record<string, string> = {
  ...Object.fromEntries(Object.entries(NOT_PAGES).filter(([p]) => !p.startsWith('/w/:workspace'))),
  ...Object.fromEntries(SURFACES.flatMap((s) => (s.flat ? [[s.path, s.flat]] : []))),
}

const paths = flattenRoutePaths(routeTable)

describe('every route lives under /w/:workspace, or says why not', () => {
  it('reads the real route table', () => {
    expect(paths).toContain('/w/:workspace/agents')
    expect(paths).toContain('/w/:workspace/walkthrough/:id')
    expect(paths).toContain('/w/:workspace/review/:id')
    expect(paths).toContain('/w/:workspace/share/:token')
    expect(paths).toContain('/w/:workspace/storyboard/:slug')
    expect(paths).toContain('/w/:workspace/narrative/:slug')
    expect(paths).toContain('/w/:workspace/ddd-release/:narrative/:runId')
  })

  it('has no flat route without a reason', () => {
    const flat = paths.filter((p) => !p.startsWith('/w/:workspace'))
    const unexplained = flat.filter((p) => !(p in FLAT_ALLOWED))
    expect(
      unexplained,
      `routes outside /w/:workspace with no reason in FLAT_ALLOWED: ${unexplained.join(', ')}. ` +
        'Put the page under /w/:workspace, or give it a `flat` reason in guide/surfaces.ts (a redirect: in NOT_PAGES).',
    ).toEqual([])
  })

  it('lists no route that does not exist', () => {
    // An allowlist entry outliving its route is how an allowlist stops being read.
    const stale = Object.keys(FLAT_ALLOWED).filter((p) => !paths.includes(p))
    expect(stale, `FLAT_ALLOWED names routes that are gone: ${stale.join(', ')}`).toEqual([])
  })

  it('has no flat artifact viewer, and forwards none', () => {
    // One URL per artifact, under its workspace (owner decision, 2026-10-08;
    // canopy-web#1337). A flat /walkthrough/, /review/, /share/, /storyboard/,
    // /narrative/ or /ddd-release/ link is a plain server 404 — not a page, and
    // not a redirect to the scoped one.
    const artifact = /^\/(walkthrough|review|share|storyboard|narrative|ddd-release)(\/|$)/
    expect(paths.filter((p) => artifact.test(p))).toEqual([])
    expect(Object.keys(FLAT_ALLOWED).filter((p) => artifact.test(p))).toEqual([])
  })

  it('has no FOLLOW-UP left: no tenant data on a flat public URL', () => {
    const debts = Object.entries(FLAT_ALLOWED).filter(([, why]) => why.startsWith('FOLLOW-UP'))
    expect(debts).toEqual([])
  })

  it('gives no page under /w/:workspace a reason to be flat', () => {
    const odd = SURFACES.filter((s) => s.flat && s.path.startsWith('/w/:workspace')).map((s) => s.path)
    expect(odd).toEqual([])
  })

  it('gives every exception a reason', () => {
    for (const [path, reason] of Object.entries(FLAT_ALLOWED)) {
      expect(reason.trim().length, `${path} has no reason`).toBeGreaterThan(10)
    }
  })
})
