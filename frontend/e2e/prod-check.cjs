// A check of chat sessions on a deployed canopy-web, in a real browser, as the
// token's owner. Read-only, except one short live chat with an agent when
// LIVE_AGENT is set (that session is archived at the end).
//
//   cd frontend && CANOPY_E2E_TOKEN=... node e2e/prod-check.cjs
//   CANOPY_E2E_TOKEN=... LIVE_AGENT=ada SEND_EARLY=1 node e2e/prod-check.cjs
//
// Env: CANOPY_E2E_TOKEN (required, never read from a file), CANOPY_URL
// (default https://canopy.dimagi.com), WORKSPACE (default dimagi), LIVE_AGENT,
// SEND_EARLY (press Enter before the chat connects). Screenshots go to
// e2e/prod-shots/. Exit 0 when every check passes.
const fs = require('fs')
const path = require('path')
const crypto = require('crypto')
const { chromium } = require('@playwright/test')

const BASE = (process.env.CANOPY_URL || 'https://canopy.dimagi.com').replace(/\/$/, '')
const WS = process.env.WORKSPACE || 'dimagi'
const TOKEN = (process.env.CANOPY_E2E_TOKEN || '').trim()
const LIVE_AGENT = process.env.LIVE_AGENT || ''
const OUT = path.join(__dirname, 'prod-shots')

if (!TOKEN) {
  console.error('no token: set CANOPY_E2E_TOKEN (scripts never default to a person\'s own PAT file)')
  process.exit(2)
}

// What it creates says it was a script (as scripts/script_provenance.py does).
const SCRIPT = 'prod-check.cjs'
const PARENT_ENV = {
  CANOPY_TURN_ID: 'X-Canopy-Parent-Turn',
  CANOPY_SESSION_ID: 'X-Canopy-Parent-Session',
  CLAUDE_SESSION_ID: 'X-Canopy-Claude-Session',
}
const scriptHeaders = () => {
  const out = {
    'X-Canopy-Client': SCRIPT,
    'User-Agent': `${SCRIPT} (canopy-web script; node ${process.version})`,
    'X-Request-Id': `prod-check-${crypto.randomUUID().slice(0, 8)}`,
  }
  for (const [env, header] of Object.entries(PARENT_ENV)) {
    if (process.env[env]?.trim()) out[header] = process.env[env].trim()
  }
  return out
}

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`)
}
const api = async (method, url, body) => {
  const res = await fetch(BASE + url, {
    method,
    headers: { ...scriptHeaders(), Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) throw new Error(`${method} ${url} → ${res.status} ${await res.text()}`)
  return res.json()
}
const atBottom = (page) =>
  page.getByTestId('chat-scroll').evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight <= 2)

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  // A browser session for the token's owner, for at most an hour.
  const minted = await api('POST', '/api/debug/mint-session/', { ttl_seconds: 3600 })
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  await ctx.addCookies([{
    name: minted.cookie_name, value: minted.cookie_value, domain: new URL(BASE).hostname,
    path: '/', secure: BASE.startsWith('https'), httpOnly: true, sameSite: 'Lax',
  }])
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(String(e)))

  // The sessions list, with the "Started by me" filter.
  await page.goto(`${BASE}/w/${WS}/sessions`)
  await page.getByTestId('sessions-panel').waitFor({ timeout: 30_000 })
  await page.waitForTimeout(2500)
  await page.screenshot({ path: `${OUT}/1-sessions.png` })
  check('sessions list renders', true)
  check('"Started by me" filter is there', (await page.getByTestId('toggle-mine-only').count()) === 1)

  // The most recently active sessions: each opens at the newest row, with no
  // "Jump to latest".
  const rows = await api('GET', `/api/w/${WS}/canopy-sessions/?state=active&limit=60`)
  for (const s of rows.filter((r) => !r.sensitive && r.agent_slug).slice(0, 6)) {
    const label = `${s.title || s.id.slice(0, 8)} (${s.agent_slug})`
    await page.goto(`${BASE}/w/${WS}/sessions/${s.id}`)
    try {
      await page.getByTestId('chat-scroll').waitFor({ timeout: 30_000 })
      await page.waitForTimeout(4000) // the REST paint, then the socket snapshot replacing it
      const bottom = await atBottom(page)
      const jump = await page.getByTestId('jump-to-latest').count()
      const shown = await page.getByTestId('message-row').count()
      const asks = await page.locator('[data-testid="message-row"][data-role="user"]').count()
      const phase = await page.getByTestId('turn-phase').getAttribute('data-phase').catch(() => null)
      // The page says working only when the server does.
      const spinner = await page.getByTestId('pending-reply').count()
      check(`opens at the newest row: ${label}`, bottom && jump === 0,
        `rows ${shown}, person's messages ${asks}, phase ${phase}, list says running=${s.running}`)
      if (!s.running) check(`no "working" line on an idle session: ${label}`, spinner === 0)
      await page.screenshot({ path: `${OUT}/2-${s.id.slice(0, 8)}.png` })
    } catch (e) {
      check(`opens: ${label}`, false, String(e).slice(0, 200))
    }
  }

  // /ide: the filter, and a header whose counts clear "New session".
  await page.goto(`${BASE}/ide`)
  await page.getByTestId('ide-filters').waitFor({ timeout: 30_000 })
  await page.getByText('Loading…', { exact: true }).waitFor({ state: 'detached', timeout: 30_000 }).catch(() => {})
  await page.getByTestId('ide-filters').click()
  await page.getByTestId('ide-mine-only').waitFor({ timeout: 5_000 }).catch(() => {})
  check('/ide Filters has "Started by me"', (await page.getByTestId('ide-mine-only').count()) === 1)
  await page.keyboard.press('Escape')
  const overlaps = await page.evaluate(() => {
    const totals = document.querySelector('[data-testid="ide-state-totals"]')
    const button = [...document.querySelectorAll('button')].find((b) => /New session/.test(b.textContent ?? ''))
    if (!totals || !button) return null
    const a = totals.getBoundingClientRect()
    const r = button.getBoundingClientRect()
    return a.right > r.left + 1 && a.top < r.bottom && a.bottom > r.top
  })
  check('/ide header: counts clear of New session', overlaps === false)
  await page.screenshot({ path: `${OUT}/3-ide.png` })

  // One live chat: send, see the reply arrive, the turn settle, the view stay down.
  if (LIVE_AGENT) {
    const s = await api('POST', `/api/w/${WS}/canopy-sessions/`,
      { agent_slug: LIVE_AGENT, title: 'production check (safe to delete)' })
    try {
      await page.goto(`${BASE}/w/${WS}/sessions/${s.id}`)
      await page.getByTestId('composer').waitFor({ timeout: 30_000 })
      if (!process.env.SEND_EARLY) await page.getByText('Connected', { exact: true }).waitFor({ timeout: 30_000 })
      await page.getByTestId('composer').fill(
        'This is an automated check of the chat page. Reply with exactly the word OK and do nothing else.')
      await page.getByTestId('composer').press('Enter')
      const t0 = Date.now()
      check('live: the activity line appears',
        await page.getByTestId('pending-reply').waitFor({ timeout: 60_000 }).then(() => true, () => false))
      const replied = await page.locator('[data-testid="message-row"][data-role="assistant"]')
        .filter({ hasText: /\bOK\b/ }).first()
        .waitFor({ timeout: 300_000 }).then(() => true, () => false)
      check('live: the reply arrives', replied, `${Math.round((Date.now() - t0) / 1000)}s`)
      check('live: the turn stops saying it is working',
        await page.getByTestId('pending-reply').waitFor({ state: 'detached', timeout: 120_000 }).then(() => true, () => false))
      check('live: the view is at the newest row', await atBottom(page))
      const before = await page.getByTestId('message-row').count()
      await page.reload()
      await page.getByTestId('chat-scroll').waitFor()
      await page.waitForTimeout(4000)
      const after = await page.getByTestId('message-row').count()
      check('live: a reload shows the same rows, no duplicates', before === after, `${before} → ${after}`)
      await page.screenshot({ path: `${OUT}/4-live.png` })
    } finally {
      await api('POST', `/api/w/${WS}/canopy-sessions/${s.id}/archive`).catch((e) =>
        console.log(`could not archive ${s.id}: ${e}`))
    }
  }

  check('no uncaught page errors', errors.length === 0, errors.slice(0, 3).join(' | '))
  await browser.close()
  const failed = results.filter((r) => !r.ok)
  console.log(`\n${results.length - failed.length}/${results.length} passed`)
  process.exit(failed.length ? 1 : 0)
})().catch((e) => {
  console.error(e)
  process.exit(2)
})
