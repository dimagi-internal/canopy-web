import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

import { expect, test } from '@playwright/test'

/** Not a test — a screenshot harness for judging the multiplayer UI by eye.
 *  Kept out of the default run by its filename (see playwright.config). */
const here = path.dirname(fileURLToPath(import.meta.url))
const authDir = path.join(here, '.auth')
const shots = path.join(here, '.shots')

const chatUrl = () =>
  `/w/dimagi/chat/${fs.readFileSync(path.join(authDir, 'mp-session-id.txt'), 'utf8').trim()}`

test('capture the multiplayer states', async ({ page, browser }) => {
  fs.mkdirSync(shots, { recursive: true })

  await page.goto(chatUrl())
  await expect(page.getByTestId('composer')).toBeVisible()
  await page.screenshot({ path: path.join(shots, '1-alone.png'), fullPage: false })

  const ctx = await browser.newContext({ storageState: path.join(authDir, 'state2.json') })
  const second = await ctx.newPage()
  await second.goto(chatUrl())
  await expect(second.getByTestId('composer')).toBeVisible()
  await expect(page.getByTestId('presence-chip')).toHaveCount(1)
  await page.screenshot({ path: path.join(shots, '2-two-present.png') })

  // Someone else typing: the lock + the "is typing" affordance.
  await second.getByTestId('composer').fill('a half-finished thought from Robin')
  await expect(page.getByTestId('composer')).toBeDisabled({ timeout: 15_000 })
  await page.screenshot({ path: path.join(shots, '3-locked-by-teammate.png') })
  await page.locator('header, .flex.items-center.gap-3.border-b').first()
    .screenshot({ path: path.join(shots, '4-presence-bar.png') })
    .catch(() => {})

  await ctx.close()
})
