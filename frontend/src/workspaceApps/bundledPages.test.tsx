// @vitest-environment jsdom
/**
 * Every page canopy ships in a bundled app (apps/workspace_apps/bundled/<app>/pages)
 * compiles in the frame's compiler and draws its first screen without crashing,
 * given an app prop whose calls resolve to nothing. A typo in a page otherwise
 * surfaces only when someone opens it.
 */
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { compile } from '@/dynamicViews/frame/compile'
import { AppKitContext, type AppApi } from '@/dynamicViews/frame/kit'

// Read through Vite (not node:fs), so the file type-checks with the app's browser lib.
const PAGES = import.meta.glob('../../../apps/workspace_apps/bundled/*/pages/*.jsx', {
  query: '?raw', import: 'default', eager: true,
}) as Record<string, string>
const MANIFESTS = import.meta.glob('../../../apps/workspace_apps/bundled/*/manifest.json', {
  import: 'default', eager: true,
}) as Record<string, Record<string, unknown>>

function pages(): [string, string][] {
  return Object.entries(PAGES).map(([path, code]) => {
    const m = path.match(/bundled\/([^/]+)\/pages\/([^/]+)$/)
    return [`${m?.[1]}/${m?.[2]}`, code] as [string, string]
  })
}

function manifest(app: string): Record<string, unknown> {
  const hit = Object.entries(MANIFESTS).find(([p]) => p.includes(`/bundled/${app}/manifest.json`))
  if (!hit) throw new Error(`no manifest for ${app}`)
  return hit[1]
}

function stubApi(app: string, page: string): AppApi {
  const m = manifest(app) as { collections: Record<string, unknown>; settings: Record<string, { default?: unknown }> }
  const nothing = async () => [] as never
  return {
    key: app, name: app, page, params: {}, roles: ['member', 'recruiter', 'interviewer', 'compliance'], public: false,
    draft: false, dataModel: m.collections, fills: {},
    settings: Object.fromEntries(Object.entries(m.settings).map(([k, s]) => [k, s.default])),
    query: nothing, act: async () => ({}), bulk: async () => ({ done: [], failed: [] }),
    link: async () => ({ url: 'https://example.org/l/x', days: 30 }), history: nothing,
    upload: async () => ({ id: 1, name: 'x' }), openFile: () => {}, go: () => {}, refresh: () => {},
    field: (c, n) => ((m.collections[c] as { fields?: Record<string, unknown> } | undefined)?.fields?.[n] as never),
    fieldLabel: (_c, n) => n, options: () => [], optionLabel: (_c, _n, v) => String(v ?? ''),
  } as AppApi
}

describe('bundled app pages', () => {
  it('finds the bundled pages', () => expect(pages().length).toBeGreaterThan(10))

  it.each(pages())('%s compiles and draws', (name, code) => {
    const [app, file] = name.split('/')
    const View = compile(code)
    const api = stubApi(app, file.replace(/\.jsx$/, ''))
    const scope = { workspace: { slug: 'dimagi', name: 'Dimagi' }, project: null,
      view: { id: 0, slug: api.page, title: api.page, version: 0 }, viewer: { email: 'rita@example.org', name: 'Rita' } }
    const { container } = render(
      <AppKitContext.Provider value={api}>
        <View data={{ record: { id: 1 } }} scope={scope} app={api} canopy={{ read: async () => null, refresh() {}, navigate() {} }} />
      </AppKitContext.Provider>,
    )
    expect(container).toBeTruthy()
  })
})
