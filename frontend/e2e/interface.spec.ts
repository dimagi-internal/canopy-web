import fs from 'fs'

import { expect, test, type Page } from '@playwright/test'


// The Callers setting (agent → Settings → Channels): a default agent says so in words, and an agent
// with a published interface (ACE's, the fixture) shows its summary and opens in the form unchanged.
// Set SHOTS=<dir> to also save screenshots of both.
const ace = JSON.parse(
  fs.readFileSync(new URL('../src/components/agents/aceInterface.fixture.json', import.meta.url), 'utf8'),
)
const shots = process.env.SHOTS
if (shots) fs.mkdirSync(shots, { recursive: true })
// The SPA shell sets Django's CSRF cookie (`ensure_csrf_cookie`); a write needs it echoed back.
const csrf = async (page: Page) => {
  await page.request.get('/')
  return (await page.context().cookies()).find((c) => c.name === 'csrftoken')?.value ?? ''
}

async function api(page: Page, method: 'PUT' | 'POST' | 'DELETE', url: string, data?: unknown) {
  const res = await page.request.fetch(url, {
    method, data, headers: { 'X-CSRFToken': await csrf(page), 'Content-Type': 'application/json' },
  })
  expect(res.ok(), `${method} ${url}: ${await res.text()}`).toBeTruthy()
  return res.json()
}

test('an agent on the default interface says "Owner and admins only (default)"', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/settings/channels')
  await expect(page.getByTestId('interface-default')).toContainText('Owner and admins only (default)')
  await expect(page.getByTestId('summary-line')).toHaveText(['Owner and admins: everything', 'Workspace editors: everything (every turn they start needs an admin\'s go-ahead)', 'Everyone else: refused'])
  if (shots) await page.getByTestId('access-summary').locator('xpath=ancestor::div[contains(@class,"divide-y")]').first().screenshot({ path: `${shots}/callers-default.png` })
  const got = await (await page.request.get('/api/agents/echo/interface')).json()
  expect(got.interface).toEqual({ version: 1, full: [], capabilities: {}, callers_default: 'none' })
})

test("ACE's interface opens in the form, saves unchanged, and resets to the default", async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/settings/channels')
  await api(page, 'PUT', '/api/agents/echo/interface', { interface: ace })
  try {
    await page.reload()
    await expect(page.getByTestId('summary-line').first()).toContainText('Owner and admins: everything')
    await expect(page.getByTestId('summary-line').filter({ hasText: 'Workspace members: ask, canopy, connect' })).toHaveCount(1)
    if (shots) await page.getByTestId('access-summary').locator('xpath=ancestor::div[contains(@class,"divide-y")]').first().screenshot({ path: `${shots}/callers-ace.png` })

    await page.getByRole('button', { name: 'Edit' }).click()
    await expect(page.getByTestId('capability-card')).toHaveCount(3)
    if (shots) await page.getByTestId('interface-editor').screenshot({ path: `${shots}/callers-ace-editor.png` })
    // Touch a field and put it back, then save: what the server holds is ACE's interface, unchanged.
    const tools = page.getByLabel('canopy tools')
    const before = await tools.inputValue()
    await tools.fill(`${before}\nExtra`)
    await tools.fill(before)
    await expect(page.getByRole('button', { name: 'Save' })).toBeEnabled()
    await page.getByRole('button', { name: 'Save' }).click()
    await expect(page.getByTestId('interface-editor')).toHaveCount(0)
    expect((await (await page.request.get('/api/agents/echo/interface')).json()).interface).toEqual(ace)

    await page.getByRole('button', { name: 'Reset to default' }).click()
    await expect(page.getByTestId('interface-default')).toBeVisible()
  } finally {
    await api(page, 'DELETE', '/api/agents/echo/interface')
  }
})

test('the parser\'s reason shows beside the field it is about', async ({ page }) => {
  await page.goto('/w/dimagi/agents/echo/settings/channels')
  await page.getByRole('button', { name: 'Add rules' }).click()
  await page.getByRole('button', { name: 'Add an ask door for colleagues' }).click()
  await page.getByLabel('ask caller domain 1').fill('not a domain')
  await expect(page.getByTestId('field-error').first()).toContainText('not a caller class')
  await expect(page.getByRole('button', { name: 'Save' })).toBeDisabled()
})
