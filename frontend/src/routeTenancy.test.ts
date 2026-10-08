// @vitest-environment jsdom
/**
 * Nothing on canopy outside a workspace (owner decision, 2026-10-08;
 * canopy-web#1289).
 *
 * Every route that shows tenant data lives under `/w/:workspace`. A route
 * outside it is allowed only if it is listed here WITH ITS REASON — so adding a
 * flat page fails this test until someone says why it may be flat, instead of
 * a share link quietly opening from any workspace again (which is how the
 * chlorine narrative sat in `dimagi` for a week with nobody able to tell).
 *
 * The reasons sort into four kinds:
 *   - personal/global: about YOU, or the fleet across your workspaces;
 *   - pre-tenant: you have no workspace yet (an invite, the first-run form);
 *   - redirect: only sends the browser to a scoped page;
 *   - FOLLOW-UP: tenant data still on a flat public URL. Each is a debt the
 *     owner decision covers but #1289 did not take on; shrink this, never grow
 *     it.
 */
import { describe, expect, it } from 'vitest'
import { routeTable } from './router'
import { flattenRoutePaths } from './guide/coverage'

const FLAT_ALLOWED: Record<string, string> = {
  // --- personal / global ---
  '/system': 'personal/global: the capability catalog, read from the canopy plugin — no tenant rows',
  '/guide': 'personal/global: the self-documenting route registry',
  '/sessions': 'personal/global: YOUR shared transcripts, across your workspaces',
  '/people/me': 'personal/global: what agents know about you, the subject',
  '/supervisor': 'personal/global: the cross-fleet Waiting on you — the fleet spans workspaces',
  '/schedules': 'personal/global: every schedule across your workspaces (tenant twin: /w/:workspace/schedules)',
  '/activity': 'personal/global: the fleet turn log across your workspaces (tenant twin: /w/:workspace/activity)',
  '/settings': 'personal/global: your own account',
  '/beta-requests': 'personal/global: requests for access to Canopy itself name no workspace',
  '/beta-requests/:requestId': 'personal/global: one request for access to Canopy itself',
  '/about': 'personal/global: the public explainer',

  // --- pre-tenant ---
  '/invite/:token': 'pre-tenant: an invitee has no membership yet, so there is no tenant to scope it under',
  '/new-workspace': 'pre-tenant: the create-a-workspace form',

  // --- redirects into a scoped page ---
  '/': 'redirect: to the active workspace',
  '/app': 'redirect: the server-loadable door into the app, to the active workspace',
  '/timeline': 'redirect: legacy flat path → /w/:workspace/timeline',
  '/shareouts/*': 'redirect: legacy flat path → /w/:workspace/shareouts',
  '/walkthroughs': 'redirect: legacy flat path → /w/:workspace/walkthroughs',
  '/storyboards': 'redirect: legacy flat path → /w/:workspace/storyboards',
  '/huddles/*': 'redirect: legacy flat path → /w/:workspace/huddles',
  '/agents/*': 'redirect: legacy flat path → /w/:workspace/agents',
  '/ddd/*': 'redirect: legacy flat path → /w/:workspace/ddd',
  '/ddd-plans': 'redirect: retired, → /',
  '/reviews': 'redirect: retired, → /',
  '/insights': 'redirect: retired, → /',
  '/projects': 'redirect: retired, → /',
  '/walkthrough/:id': "redirect: links sent before #1289 → /w/:workspace/walkthrough/:id (the row's workspace)",
  '/review/:id': "redirect: links sent before #1289 → /w/:workspace/review/:id (the row's workspace)",
  '/share/:token': "redirect: links sent before #1289 → /w/:workspace/share/:token (the row's workspace)",
  '/*': 'the not-found catch-all',

  // --- FOLLOW-UP: tenant data still on a flat public URL ---
  '/ddd-release/:narrative/:runId': 'FOLLOW-UP: a DDD run release page; not yet moved under its workspace',
  '/storyboard/:slug': 'FOLLOW-UP: a shared storyboard; not yet moved under its workspace',
  '/narrative/:slug': 'FOLLOW-UP: the per-narrative reviewer surface; not yet moved under its workspace',
}

const paths = flattenRoutePaths(routeTable)

describe('every route lives under /w/:workspace, or says why not', () => {
  it('reads the real route table', () => {
    expect(paths).toContain('/w/:workspace/agents')
    expect(paths).toContain('/w/:workspace/walkthrough/:id')
    expect(paths).toContain('/w/:workspace/review/:id')
    expect(paths).toContain('/w/:workspace/share/:token')
  })

  it('has no flat route without a reason', () => {
    const flat = paths.filter((p) => !p.startsWith('/w/:workspace'))
    const unexplained = flat.filter((p) => !(p in FLAT_ALLOWED))
    expect(
      unexplained,
      `routes outside /w/:workspace with no reason in FLAT_ALLOWED: ${unexplained.join(', ')}. ` +
        'Put the page under /w/:workspace, or add it with the reason it may be flat.',
    ).toEqual([])
  })

  it('lists no route that does not exist', () => {
    // An allowlist entry outliving its route is how an allowlist stops being read.
    const stale = Object.keys(FLAT_ALLOWED).filter((p) => !paths.includes(p))
    expect(stale, `FLAT_ALLOWED names routes that are gone: ${stale.join(', ')}`).toEqual([])
  })

  it('gives every exception a reason', () => {
    for (const [path, reason] of Object.entries(FLAT_ALLOWED)) {
      expect(reason.trim().length, `${path} has no reason`).toBeGreaterThan(10)
    }
  })
})
