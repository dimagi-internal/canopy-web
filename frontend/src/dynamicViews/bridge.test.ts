import { describe, expect, it } from 'vitest'
import { resolveNavigation, resolveRead, type CatalogModel } from './bridge'

const CATALOG: CatalogModel[] = [
  { name: 'tasks', path: '/tasks/', description: '', params: ['project', 'category', 'limit'], path_params: [] },
  { name: 'agent', path: '/agents/{slug}/', description: '', params: [], path_params: ['slug'] },
  { name: 'members', path: '/workspaces/{workspace}/members/', description: '', params: [], path_params: [] },
  { name: 'milestones', path: '/workspace-projects/{workspace}/{handle}/milestones/', description: '', params: [], path_params: ['handle'] },
]
const WS = { workspace: 'dimagi', project: null }
const PROJECT = { workspace: 'dimagi', project: 'unga-2026' }

describe('resolveRead', () => {
  it('reads a catalog model under the view’s workspace, as a GET path', () => {
    expect(resolveRead(CATALOG, WS, { model: 'tasks', params: { category: 'started', limit: 50 } }))
      .toEqual({ ok: true, url: '/api/w/dimagi/tasks/?category=started&limit=50' })
    expect(resolveRead(CATALOG, WS, { model: 'members' })).toEqual({ ok: true, url: '/api/w/dimagi/workspaces/dimagi/members/' })
  })

  it('refuses anything not in the catalog', () => {
    expect(resolveRead(CATALOG, WS, { model: 'vault' })).toEqual({ ok: false, error: "unknown model 'vault'" })
    expect(resolveRead(CATALOG, WS, { model: 'tasks', params: { secret: 'x' } }).ok).toBe(false)
  })

  it('never lets the frame choose the workspace', () => {
    expect(resolveRead(CATALOG, WS, { model: 'members', path: { workspace: 'other' } }))
      .toEqual({ ok: false, error: "members has no path parameter 'workspace'" })
  })

  it('refuses a path parameter that is not a plain identifier', () => {
    for (const slug of ['../vault', 'a/b', '', '%2e%2e', 'x?y=1']) {
      expect(resolveRead(CATALOG, WS, { model: 'agent', path: { slug } }).ok).toBe(false)
    }
    expect(resolveRead(CATALOG, WS, { model: 'agent', path: { slug: 'eva' } })).toEqual({ ok: true, url: '/api/w/dimagi/agents/eva/' })
  })

  it('fills a project view’s own handle, and needs one on a workspace view', () => {
    expect(resolveRead(CATALOG, PROJECT, { model: 'milestones' }))
      .toEqual({ ok: true, url: '/api/w/dimagi/workspace-projects/dimagi/unga-2026/milestones/' })
    expect(resolveRead(CATALOG, WS, { model: 'milestones' })).toEqual({ ok: false, error: 'milestones needs path.handle' })
  })

  it('takes only scalar parameter values', () => {
    expect(resolveRead(CATALOG, WS, { model: 'tasks', params: { project: { $ne: 1 } } }).ok).toBe(false)
    expect(resolveRead(CATALOG, WS, { model: 'tasks', params: { category: ['started', 'unstarted'] } }))
      .toEqual({ ok: true, url: '/api/w/dimagi/tasks/?category=started%2Cunstarted' })
  })
})

describe('resolveNavigation', () => {
  it('keeps app paths in canopy, under the view’s workspace', () => {
    expect(resolveNavigation('dimagi', '/tasks/CON-1')).toEqual({ kind: 'app', to: '/w/dimagi/tasks/CON-1' })
    expect(resolveNavigation('dimagi', '/w/connect/projects/x')).toEqual({ kind: 'app', to: '/w/connect/projects/x' })
  })

  it('refuses API addresses, protocol-relative and non-https links', () => {
    for (const p of ['/api/tasks/', '//evil.example/x', 'javascript:alert(1)', 'http://example.com', 'tasks']) {
      expect(resolveNavigation('dimagi', p)).toEqual({ kind: 'refused' })
    }
    expect(resolveNavigation('dimagi', 'https://example.com/a')).toEqual({ kind: 'external', url: 'https://example.com/a' })
  })

  it('lets a mailto: link out (confirmed, to the viewer\'s own mail app)', () => {
    expect(resolveNavigation('dimagi', 'mailto:leave@list.example?subject=unsubscribe'))
      .toEqual({ kind: 'external', url: 'mailto:leave@list.example?subject=unsubscribe' })
    expect(resolveNavigation('dimagi', 'mailto:a b')).toEqual({ kind: 'refused' })
  })
})
