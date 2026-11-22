import { expect, test, type Page } from '@playwright/test'

// A workspace app's BUNDLE page (apps/workspace_apps/bundles.py): plain HTML + JS files, uploaded with
// the app, drawn in canopy's sandbox with canopy's client injected. This drives the real thing: the
// page's own script writes and reads through `canopy.app` (the bridge to the canopy page), its
// sandbox refuses the network, eval and storage, and `canopy.open` moves to another page of the app.
const csrf = async (page: Page) => {
  await page.request.get('/')
  return (await page.context().cookies()).find((c) => c.name === 'csrftoken')?.value ?? ''
}

async function api(page: Page, method: 'POST' | 'DELETE', url: string, data?: unknown) {
  const res = await page.request.fetch(url, {
    method, data, headers: { 'X-CSRFToken': await csrf(page), 'Content-Type': 'application/json' },
  })
  expect(res.ok(), `${method} ${url}: ${await res.text()}`).toBeTruthy()
  return res.status() === 204 ? null : res.json()
}

const BOARD_JS = `
const out = (id, text) => { document.getElementById(id).textContent = text }
const probe = async (id, run) => { try { out(id, String(await run())) } catch (e) { out(id, 'blocked') } }
probe('eval', () => eval('"eval-ran"'))
probe('storage', () => { localStorage.setItem('k', '1'); return 'stored' })
probe('fetch-api', async () => (await fetch('/api/me/')).status)
probe('fetch-own', async () => (await (await fetch('../shared/data.json')).json()).hello)
document.getElementById('open').addEventListener('click', () => canopy.open('detail/index.html?id=7'))
canopy.ready.then(async ({ app }) => {
  out('who', app.key + ':' + app.page)
  await app.act('add_note', { input: { body: 'written by a bundle page' } })
  const rows = await app.query('note')
  out('notes', rows.map((r) => r.body).join('|'))
})
`

const DETAIL_JS = `
canopy.ready.then(({ app }) => {
  document.getElementById('params').textContent = location.search + ' ' + app.params.id
})
`

test('a bundle page reaches canopy only through the bridge, and opens another of its pages', async ({ page }) => {
  const key = `bnd-${Date.now().toString(36)}`
  await api(page, 'POST', '/api/workspace-apps/dimagi/', {
    manifest: {
      key, name: 'Bundle e2e',
      collections: { note: { fields: { body: { type: 'text' } } } },
      actions: { add_note: { role: 'member', op: 'create', collection: 'note', input: ['body'] } },
      pages: [
        { slug: 'board', title: 'Board', kind: 'bundle', entry: 'board/index.html' },
        { slug: 'detail', title: 'Detail', kind: 'bundle', entry: 'detail/index.html' },
      ],
    },
    files: [
      { path: 'board/index.html', text: '<!doctype html><html><head><link rel="stylesheet" href="../shared/app.css"></head><body>'
          + '<p id="who"></p><p id="notes"></p><p id="eval"></p><p id="storage"></p><p id="fetch-api"></p><p id="fetch-own"></p>'
          + '<button id="open">Open detail</button><script src="board.js"></script></body></html>' },
      { path: 'board/board.js', text: BOARD_JS },
      { path: 'detail/index.html', text: '<html><body><p id="params"></p><script src="detail.js"></script></body></html>' },
      { path: 'detail/detail.js', text: DETAIL_JS },
      { path: 'shared/app.css', text: 'body { font-family: sans-serif }' },
      { path: 'shared/data.json', text: '{"hello": "own-file"}' },
    ],
  })
  try {
    await page.goto(`/w/dimagi/apps/${key}/board`)
    const board = page.frameLocator('iframe[title="Board"]')
    await expect(board.locator('#who')).toHaveText(`${key}:board`)
    await expect(board.locator('#notes')).toHaveText('written by a bundle page')
    await expect(board.locator('#eval')).toHaveText('blocked')
    await expect(board.locator('#storage')).toHaveText('blocked')
    await expect(board.locator('#fetch-api')).toHaveText('blocked')
    await expect(board.locator('#fetch-own')).toHaveText('own-file')

    await board.locator('#open').click()
    await expect(page).toHaveURL(new RegExp(`/w/dimagi/apps/${key}/detail\\?id=7$`))
    await expect(page.frameLocator('iframe[title="Detail"]').locator('#params')).toHaveText('?id=7 7')
  } finally {
    await api(page, 'DELETE', `/api/workspace-apps/dimagi/${key}`)
  }
})
