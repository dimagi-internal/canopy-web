import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

import { expect, test, type Browser, type Page } from '@playwright/test'

/**
 * Two browsers, two identities, ONE chat session.
 *
 * Everything multiplayer means is a statement about somebody who is not you —
 * "nobody else here", "Another teammate is editing…", "take over". A suite with
 * one identity can open two browsers and exercise none of it, which is why this
 * surface shipped with no e2e coverage at all: the co-edited draft, the lock,
 * the takeover and the presence chips were only ever checked by unit tests
 * holding a hand-built props object.
 *
 * So this drives the real socket (`ws/canopy-sessions/{id}/`) against the real
 * consumer, twice, and asserts what each browser sees about the OTHER one.
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

  test("one person's typing appears in the other's composer", async ({ page, browser }) => {
    const second = await bothInTheRoom(page, browser)

    await page.getByTestId('composer').fill('half a thought from Alex')
    // The co-edited draft: the SERVER's copy, echoed to everyone else.
    await expect(second.getByTestId('composer')).toHaveValue('half a thought from Alex', {
      timeout: 15_000,
    })

    await second.context().close()
  })

  test('a teammate holding the draft locks the other composer, and takeover frees it',
    async ({ page, browser }) => {
      const second = await bothInTheRoom(page, browser)

      await page.getByTestId('composer').fill('I am holding this')

      // The lock is what stops two people overwriting each other mid-sentence.
      const theirBox = second.getByTestId('composer')
      await expect(theirBox).toBeDisabled({ timeout: 15_000 })
      await expect(theirBox).toHaveAttribute('placeholder', /Another teammate is editing/)

      // And it must be escapable, or one idle tab wedges the session for
      // everyone. The holder is shown as editing while the lock is live.
      await expect(page.getByTestId('presence-chip')).toHaveAttribute('data-editing', 'false')
      await expect(second.getByTestId('presence-chip')).toHaveAttribute('data-editing', 'true')

      // The composer is where their words land, so it is where the explanation
      // has to be. Their draft arrives INSIDE a disabled textarea, which renders
      // muted — pixel-identical to a placeholder — so without attribution the
      // headline feature of multiplayer reads as an empty box.
      const banner = second.getByTestId('coedit-banner')
      await expect(banner).toBeVisible()
      await expect(banner).toContainText('Alex Kim')
      await expect(banner).toContainText('you are seeing their draft')
      // And exactly ONE way to escape the lock, next to the sentence that
      // explains why you would.
      await expect(second.getByTestId('take-over')).toHaveCount(1)

      await second.getByTestId('take-over').click()
      await expect(theirBox).toBeEnabled({ timeout: 15_000 })
      await theirBox.fill('actually, mine now')
      await expect(page.getByTestId('composer')).toHaveValue('actually, mine now', {
        timeout: 15_000,
      })

      await second.context().close()
    })

  test('a lock goes idle on its own so nobody is wedged', async ({ page, browser }) => {
    const second = await bothInTheRoom(page, browser)

    await page.getByTestId('composer').fill('typing then stopping')
    await expect(second.getByTestId('composer')).toBeDisabled({ timeout: 15_000 })

    // Idle after ~2s of no edits (drafts.isDraftIdle). Without this a person who
    // wanders off mid-sentence holds the room until they close the tab, and
    // "take over" becomes mandatory rather than an escape hatch.
    await expect(second.getByTestId('composer')).toBeEnabled({ timeout: 15_000 })

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
