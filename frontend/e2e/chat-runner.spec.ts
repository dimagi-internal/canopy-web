import { expect, test, type Page } from '@playwright/test'

/**
 * A chat with a runner behind it, in a real browser.
 *
 * `e2e/fake_runner.py` answers every send the way a production runner does —
 * claim, a real Claude transcript, rows shipped with `canopy_transcript` — so
 * these drive the server's ingest, the socket, the reducer and the rendering as
 * they run for a person. The message text picks what the agent does
 * (`e2e:tools N`, `e2e:slow N`, `e2e:hang`, `e2e:newfile`; see the runner).
 *
 * Each check is something a person sees: the reply shows up once and in order,
 * the view sits at the newest message, a finished turn stops saying it is
 * working, history you loaded survives a dropped connection.
 */

const WS = 'dimagi'

async function csrf(page: Page): Promise<string> {
  await page.request.get('/api/csrf/')
  return (await page.context().cookies()).find((c) => c.name === 'csrftoken')?.value ?? ''
}

async function api(page: Page, method: 'GET' | 'POST', url: string, data?: unknown) {
  const res = await page.request.fetch(url, {
    method, data, headers: { 'X-CSRFToken': await csrf(page), 'Content-Type': 'application/json' },
  })
  expect(res.ok(), `${method} ${url} → ${res.status()} ${await res.text()}`).toBeTruthy()
  return res.json()
}

async function newSession(page: Page): Promise<string> {
  return (await api(page, 'POST', '/api/canopy-sessions/', { agent_slug: 'echo', workspace: WS })).id
}

const sessionUrl = (id: string) => `/w/${WS}/sessions/${id}`

/** Send through the API (not the composer) and wait for the runner to finish the turn. */
async function sendAndSettle(page: Page, id: string, text: string) {
  const { turn_id } = await api(page, 'POST', `/api/canopy-sessions/${id}/send`,
    { text, client_id: `api-${Date.now()}-${Math.random()}` })
  await expect.poll(async () => (await api(page, 'GET', `/api/harness/turns/${turn_id}`)).status,
    { timeout: 30_000 }).toMatch(/done|failed|cancelled/)
}

async function sendFromComposer(page: Page, text: string) {
  const box = page.getByTestId('composer')
  await box.fill(text)
  await box.press('Enter')
}

/** The transcript is scrolled to its newest content. */
async function atBottom(page: Page): Promise<boolean> {
  return page.getByTestId('chat-scroll').evaluate(
    (el) => el.scrollHeight - el.scrollTop - el.clientHeight <= 2)
}

const transcript = (page: Page) => page.getByTestId('chat-scroll')

test.describe('chat with a runner', () => {
  test('a reply arrives live, once, after what was asked', async ({ page }) => {
    const id = await newSession(page)
    await page.goto(sessionUrl(id))
    await expect(page.getByTestId('composer')).toBeVisible()
    await sendFromComposer(page, 'hello there')

    const reply = transcript(page).getByText('echo: hello there')
    await expect(reply).toBeVisible({ timeout: 20_000 })
    await expect(transcript(page).getByText('hello there', { exact: true })).toHaveCount(1)
    const text = await transcript(page).innerText()
    expect(text.indexOf('hello there')).toBeLessThan(text.indexOf('echo: hello there'))
  })

  test('a finished turn stops saying it is working', async ({ page }) => {
    const id = await newSession(page)
    await page.goto(sessionUrl(id))
    await expect(page.getByTestId('composer')).toBeVisible()
    await sendFromComposer(page, 'e2e:slow 3')

    await expect(page.getByTestId('pending-reply')).toBeVisible({ timeout: 15_000 })
    await expect(transcript(page).getByText('done after 3 tool calls')).toBeVisible({ timeout: 20_000 })
    await expect(page.getByTestId('pending-reply')).toHaveCount(0, { timeout: 10_000 })
    await expect(page.getByRole('button', { name: /^stop/i })).toHaveCount(0)
  })

  test('watching a long turn keeps the view at the newest row', async ({ page }) => {
    const id = await newSession(page)
    await page.goto(sessionUrl(id))
    await expect(page.getByTestId('composer')).toBeVisible()
    await sendFromComposer(page, 'e2e:slow 8')

    await expect(transcript(page).getByText('done after 8 tool calls')).toBeVisible({ timeout: 30_000 })
    await expect.poll(() => atBottom(page)).toBe(true)
    await expect(page.getByTestId('jump-to-latest')).toHaveCount(0)
  })

  test('opening a tool-heavy chat shows what was asked, at the bottom', async ({ page }) => {
    test.setTimeout(120_000)
    const id = await newSession(page)
    for (const n of [41, 42, 43]) await sendAndSettle(page, id, `e2e:tools ${n}`)

    await page.goto(sessionUrl(id))
    await expect(transcript(page).getByText('done after 43 tool calls')).toBeVisible()
    // Each turn is 87 rows, so a 60-row tail would hold none of these asks.
    for (const n of [41, 42, 43]) {
      await expect(transcript(page).getByText(`e2e:tools ${n}`, { exact: true })).toHaveCount(1)
    }
    await expect.poll(() => atBottom(page)).toBe(true)
    await expect(page.getByTestId('jump-to-latest')).toHaveCount(0)
  })

  test('history you loaded survives a dropped connection', async ({ page, context }) => {
    test.setTimeout(180_000)
    const id = await newSession(page)
    for (const n of [1, 2, 3, 4, 5]) await sendAndSettle(page, id, `e2e:tools 40 #${n}`)

    await page.goto(sessionUrl(id))
    await expect(transcript(page).getByText('e2e:tools 40 #5', { exact: true })).toBeVisible()
    await expect(transcript(page).getByText('e2e:tools 40 #1', { exact: true })).toHaveCount(0)

    await page.getByTestId('chat-session-menu').click()
    await page.getByTestId('menu-load-all').click()
    await expect(transcript(page).getByText('e2e:tools 40 #1', { exact: true })).toHaveCount(1)

    await context.setOffline(true)
    await page.waitForTimeout(1500)
    await context.setOffline(false)
    await sendAndSettle(page, id, 'after the drop')
    await expect(transcript(page).getByText('echo: after the drop')).toBeVisible({ timeout: 20_000 })
    await expect(transcript(page).getByText('e2e:tools 40 #1', { exact: true })).toHaveCount(1)
  })

  test('a conversation that moves to a new transcript stays in order', async ({ page }) => {
    const id = await newSession(page)
    await page.goto(sessionUrl(id))
    await expect(page.getByTestId('composer')).toBeVisible()
    await sendFromComposer(page, 'first ask')
    await expect(transcript(page).getByText('echo: first ask')).toBeVisible({ timeout: 20_000 })
    await sendFromComposer(page, 'e2e:newfile')
    await expect(transcript(page).getByText('echo: e2e:newfile')).toBeVisible({ timeout: 20_000 })

    const order = async () => {
      const text = await transcript(page).innerText()
      return [text.indexOf('echo: first ask'), text.indexOf('echo: e2e:newfile')]
    }
    const [a, b] = await order()
    expect(a).toBeGreaterThanOrEqual(0)
    expect(a).toBeLessThan(b)

    await page.reload()
    await expect(transcript(page).getByText('echo: e2e:newfile')).toBeVisible()
    const [a2, b2] = await order()
    expect(a2).toBeLessThan(b2)
    await expect(transcript(page).getByText('echo: first ask')).toHaveCount(1)
    await expect(transcript(page).getByText('echo: e2e:newfile')).toHaveCount(1)
  })

  test('a send from another tab shows once in both', async ({ page, context }) => {
    const id = await newSession(page)
    await page.goto(sessionUrl(id))
    const other = await context.newPage()
    await other.goto(sessionUrl(id))
    await expect(page.getByTestId('composer')).toBeVisible()
    await expect(other.getByTestId('composer')).toBeVisible()

    await sendFromComposer(page, 'from tab one')
    for (const p of [page, other]) {
      await expect(transcript(p).getByText('echo: from tab one')).toBeVisible({ timeout: 20_000 })
      await expect(transcript(p).getByText('from tab one', { exact: true })).toHaveCount(1)
    }
  })

  test('the sessions list says a working cloud session is running', async ({ page }) => {
    const id = await newSession(page)
    await api(page, 'POST', `/api/canopy-sessions/${id}/send`, { text: 'e2e:slow 10', client_id: `l-${Date.now()}` })
    await expect.poll(async () => {
      const rows = await api(page, 'GET', `/api/canopy-sessions/?workspace=${WS}`)
      return rows.find((s: { id: string }) => s.id === id)?.running
    }, { timeout: 15_000 }).toBe(true)
  })
})
