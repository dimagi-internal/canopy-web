import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

import { devices, expect, test, type Browser, type BrowserContextOptions, type Page } from '@playwright/test'

/** Not a test — a screenshot harness for judging the multiplayer UI by eye.
 *  Kept out of the default run by its filename (see playwright.config).
 *
 *  Run it explicitly:  npx playwright test -c playwright.shots.config.ts
 *  Shots land in e2e/.shots/ (or $MP_SHOTS_DIR), one set per variant — dark
 *  desktop, light desktop, dark phone — because "is it noticeable" has to hold
 *  in both themes and at phone width, not just in the one a developer uses.
 *
 *  Alex (the first user) is the viewer; Robin (the second) is the one typing.
 *  Waits are on Robin's TEXT, not a test id, so the same harness photographs
 *  any version of the typing UI — that is how the before/after pairs in a PR
 *  are taken. */
const here = path.dirname(fileURLToPath(import.meta.url))
const authDir = path.join(here, '.auth')
const shots = process.env.MP_SHOTS_DIR ?? path.join(here, '.shots')

const chatUrl = () =>
  `/w/dimagi/chat/${fs.readFileSync(path.join(authDir, 'mp-session-id.txt'), 'utf8').trim()}`

const LONG =
  'Also flag Partner D\'s visit counts for the last two weeks — they jumped from about forty a day ' +
  'to over a hundred and twenty, and nobody on the call could explain it. I want the raw rows, ' +
  'not the dashboard, and whether the GPS points cluster around one place'

interface Variant {
  name: string
  theme: 'dark' | 'light'
  device?: BrowserContextOptions
}

const VARIANTS: Variant[] = [
  { name: 'desktop-dark', theme: 'dark' },
  { name: 'desktop-light', theme: 'light' },
  { name: 'phone-dark', theme: 'dark', device: devices['Pixel 7'] },
]

async function open(browser: Browser, v: Variant, state: string): Promise<Page> {
  const ctx = await browser.newContext({
    ...(v.device ?? { viewport: { width: 1280, height: 800 } }),
    storageState: path.join(authDir, state),
  })
  await ctx.addInitScript((t) => localStorage.setItem('theme', t), v.theme)
  const page = await ctx.newPage()
  await page.goto(chatUrl())
  await expect(page.getByTestId('composer')).toBeVisible()
  return page
}

for (const v of VARIANTS) {
  test(`capture the multiplayer states — ${v.name}`, async ({ browser }) => {
    fs.mkdirSync(shots, { recursive: true })
    const shot = (page: Page, n: string) =>
      page.screenshot({ path: path.join(shots, `${v.name}-${n}.png`) })

    const alex = await open(browser, v, 'state.json')
    const robin = await open(browser, v, 'state2.json')
    await expect(alex.getByTestId('presence-chip')).toHaveCount(1)
    await shot(alex, '1-two-present')

    // Robin typing a short line, words shared.
    await robin.getByTestId('composer').fill('Also flag Partner D\'s visit counts')
    await expect(alex.getByText('Also flag Partner D\'s visit counts')).toBeVisible({ timeout: 15_000 })
    await shot(alex, '2-peer-typing')

    // A long draft — wraps, then caps.
    await robin.getByTestId('composer').fill(LONG)
    await expect(alex.getByText(/whether the GPS points/)).toBeAttached({ timeout: 15_000 })
    await shot(alex, '3-peer-typing-long')

    // Robin sends: the line lands in Alex's transcript under Robin's name.
    await robin.getByTestId('composer').fill('Robin here — can you pull the Partner D rows?')
    await robin.getByTestId('send').click()
    // .last(): every variant sends it into the same session.
    await expect(alex.getByText('Robin here — can you pull the Partner D rows?').last()).toBeVisible({ timeout: 15_000 })
    await shot(alex, '4-peer-message')

    // Robin switches to "Typing…": Alex sees the fact of typing, not the words.
    await robin.getByTestId('chat-session-menu').click()
    await robin.getByRole('menuitemradio', { name: 'Typing…' }).click()
    await robin.keyboard.press('Escape')
    await robin.getByTestId('composer').fill('words Alex should never see')
    await expect(alex.getByText(/is typing/)).toBeVisible({ timeout: 15_000 })
    await alex.waitForTimeout(400)
    await shot(alex, '5-peer-typing-withheld')

    // The mode is stored on Robin's server-side draft, so put it back or the
    // next variant opens with Robin already withholding.
    await robin.getByTestId('composer').fill('')
    await robin.getByTestId('chat-session-menu').click()
    await robin.getByRole('menuitemradio', { name: 'My text' }).click()
    await robin.keyboard.press('Escape')
    await robin.context().close()
    await alex.context().close()
  })
}
