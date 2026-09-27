import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

import { expect, test, type Browser, type Page } from '@playwright/test'

/**
 * Two browsers, two identities, ONE chat session.
 *
 * Everything multiplayer means is a statement about somebody who is not you —
 * "nobody else here", "Bo B is typing: …", a queued send with somebody else's
 * name on it. A suite with one identity can open two browsers and exercise
 * none of it, which is why this surface shipped with no e2e coverage at all:
 * the live typing row, the queued-sends list and the presence chips were only
 * ever checked by unit tests holding a hand-built props object.
 *
 * So this drives the real socket (`ws/canopy-sessions/{id}/`) against the real
 * consumer, twice, and asserts what each browser sees about the OTHER one.
 *
 * As of 0.13, every editor has their OWN draft — there is no shared lock, no
 * co-edit banner and no take-over button to test (canopy-ui#… "own composer
 * per person"). A teammate's live text arrives as a `typing-row` above the
 * composer instead, and the composer itself is never disabled by anyone
 * else's typing.
 *
 * Deliberately NOT asserted here: an agent reply. A send enqueues a Turn that a
 * session-capable runner drives, and there is no runner in e2e — asserting a
 * reply would mean faking one, which tests the fake. Everything below is the
 * part that is genuinely canopy-web's own.
 */

const here = path.dirname(fileURLToPath(import.meta.url))
const authDir = path.join(here, '.auth')
const state2 = path.join(authDir, 'state2.json')

const sessionId = () =>
  fs.readFileSync(path.join(authDir, 'mp-session-id.txt'), 'utf8').trim()

const chatUrl = () => `/w/dimagi/chat/${sessionId()}`

/** A second browser context authenticated as the OTHER user. */
async function openAsSecondUser(browser: Browser): Promise<Page> {
  const ctx = await browser.newContext({ storageState: state2 })
  return ctx.newPage()
}

/** Both browsers on the same session, each with a live socket. */
async function bothInTheRoom(page: Page, browser: Browser): Promise<Page> {
  await page.goto(chatUrl())
  await expect(page.getByTestId('composer')).toBeVisible()

  const second = await openAsSecondUser(browser)
  await second.goto(chatUrl())
  await expect(second.getByTestId('composer')).toBeVisible()

  // Presence is the handshake: until each side has seen the other, any
  // assertion about the draft lock is racing the socket rather than testing it.
  await expect(page.getByTestId('presence-chip')).toHaveCount(1)
  await expect(second.getByTestId('presence-chip')).toHaveCount(1)
  return second
}

test.describe('multiplayer chat', () => {
  test('alone in a session, the room says so', async ({ page }) => {
    await page.goto(chatUrl())
    await expect(page.getByTestId('composer')).toBeVisible()
    await expect(page.getByTestId('presence-empty')).toBeVisible()
    await expect(page.getByTestId('presence-chip')).toHaveCount(0)
  })

  test('each browser sees the other person arrive', async ({ page, browser }) => {
    const second = await bothInTheRoom(page, browser)

    // Each sees exactly ONE other person — never themselves. Self-in-presence
    // is the obvious way to get this wrong and it looks fine until two people
    // are actually in a room.
    await expect(page.getByTestId('presence-chip')).toHaveCount(1)
    await expect(second.getByTestId('presence-chip')).toHaveCount(1)
    const mineSaysTheirs = await page.getByTestId('presence-chip').getAttribute('data-user-id')
    const theirsSaysMine = await second.getByTestId('presence-chip').getAttribute('data-user-id')
    expect(mineSaysTheirs).not.toBe(theirsSaysMine)

    await second.context().close()
    // And leaving is observable, or a room slowly fills with ghosts.
    await expect(page.getByTestId('presence-empty')).toBeVisible({ timeout: 15_000 })
  })

  test("one person's typing appears as a typing row for the other, and never touches their own composer",
    async ({ page, browser }) => {
      const second = await bothInTheRoom(page, browser)

      await page.getByTestId('composer').fill('half a thought from Alex')
      // Everyone has their OWN draft now (canopy-ui 0.13) — a teammate's live
      // text shows as a row above the composer, never inside your own box.
      await expect(second.getByTestId('typing-row')).toContainText('half a thought from Alex', {
        timeout: 15_000,
      })
      await expect(second.getByTestId('composer')).toHaveValue('')
      await expect(second.getByTestId('composer')).toBeEnabled()

      await second.context().close()
    })

  test("a teammate's live typing never locks the other composer", async ({ page, browser }) => {
    const second = await bothInTheRoom(page, browser)

    await page.getByTestId('composer').fill('I am typing this')

    const theirBox = second.getByTestId('composer')
    await expect(second.getByTestId('typing-row')).toContainText('I am typing this', {
      timeout: 15_000,
    })
    // Never locked: there is no shared draft to co-edit any more, so the
    // other box stays fully theirs to type into the whole time — no
    // co-edit banner, no take-over button, because there is nothing to take
    // over.
    await expect(theirBox).toBeEnabled()
    await expect(second.getByTestId('coedit-banner')).toHaveCount(0)
    await expect(second.getByTestId('take-over')).toHaveCount(0)

    // And it really is their OWN box: typing into it neither merges with nor
    // gets overwritten by what the first person is typing.
    await theirBox.fill('actually, my own words')
    await expect(theirBox).toHaveValue('actually, my own words')
    await expect(page.getByTestId('composer')).toHaveValue('I am typing this')

    await second.context().close()
  })

  test('a typing row disappears once the person stops (clears their draft)', async ({ page, browser }) => {
    const second = await bothInTheRoom(page, browser)

    await page.getByTestId('composer').fill('typing then stopping')
    await expect(second.getByTestId('typing-row')).toContainText('typing then stopping', {
      timeout: 15_000,
    })

    // An empty `draft.typing` body is the peer's "I stopped" signal
    // (sessionReducer drops the row rather than showing a blank one).
    await page.getByTestId('composer').fill('')
    await expect(second.getByTestId('typing-row')).toHaveCount(0, { timeout: 15_000 })

    await second.context().close()
  })

  test('a message sent by one lands in the other transcript', async ({ page, browser }) => {
    const second = await bothInTheRoom(page, browser)

    await page.getByTestId('composer').fill('shared message from Alex')
    await page.getByTestId('send').click()

    // The sender's box clears optimistically…
    await expect(page.getByTestId('composer')).toHaveValue('', { timeout: 15_000 })
    // …and the message is in BOTH transcripts. This is the payoff: one session,
    // two people, one shared history.
    await expect(page.getByText('shared message from Alex')).toBeVisible({ timeout: 15_000 })
    await expect(second.getByText('shared message from Alex')).toBeVisible({ timeout: 15_000 })

    await second.context().close()
  })
})
