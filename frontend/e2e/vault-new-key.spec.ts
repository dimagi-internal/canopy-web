import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'
import { expect, test, type Browser, type CDPSession, type Page } from '@playwright/test'

// "Create a new vault key" as a person meets it: real API, real crypto in the browser,
// real passkey ceremonies against a Chromium virtual authenticator. Each scenario has
// its own person (seed.py) who already holds a key sealed by a passphrase nobody knows.
// `hasPrf: false` behaves like a password manager's passkey: it signs, but gives no PRF
// output, so it cannot hold a vault key.

const here = path.dirname(fileURLToPath(import.meta.url))
const NEW_PASSPHRASE = 'a new passphrase for e2e'
const ANOTHER_PASSPHRASE = 'yet another passphrase e2e'
const WELL_FORMED_SECRET_KEY = 'CV1-0000-0000-0000-0000-0000-0000-00'
const WAY_OUT = 'Lost your passphrase or Secret Key? Create a new vault key'

const sessionCookie = (file: string) => ({
  name: 'sessionid', value: fs.readFileSync(path.join(here, '.auth', `${file}.txt`), 'utf8').trim(),
  domain: 'localhost', path: '/', expires: Math.floor(Date.now() / 1000) + 86400,
  httpOnly: true, secure: false, sameSite: 'Lax' as const,
})

async function person(browser: Browser, name: string, { prf }: { prf: boolean | null }): Promise<Page> {
  const context = await browser.newContext({
    baseURL: 'http://localhost:3000',
    storageState: { cookies: [sessionCookie(name)], origins: [] },
  })
  // Whether the unlock prompt EVER appeared, however briefly: the promise under test is
  // that the old key is never asked for, which a check at the end would miss.
  await context.addInitScript(() => {
    new MutationObserver(() => {
      if (document.querySelector('[data-testid="vault-unlock"]')) sessionStorage.setItem('saw-unlock', '1')
    }).observe(document, { childList: true, subtree: true })
  })
  const page = await context.newPage()
  if (prf !== null) {
    const cdp = await context.newCDPSession(page)
    await cdp.send('WebAuthn.enable')
    authenticators.set(page, { cdp, id: await addAuthenticator(cdp, prf) })
  }
  return page
}

const authenticators = new WeakMap<Page, { cdp: CDPSession; id: string }>()

async function addAuthenticator(cdp: CDPSession, prf: boolean): Promise<string> {
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', { options: {
    protocol: 'ctap2', ctap2Version: 'ctap2_1', transport: 'internal', hasResidentKey: true,
    hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true, hasPrf: prf,
  } })
  return authenticatorId
}

/** The same passkey, now giving no PRF output: it said it could at registration, or never
 *  said (a password manager's), so only an actual unlock finds out. */
async function passkeyStopsGivingPrf(page: Page) {
  const { cdp, id } = authenticators.get(page)!
  const { credentials } = await cdp.send('WebAuthn.getCredentials', { authenticatorId: id })
  await cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId: id })
  const next = await addAuthenticator(cdp, false)
  for (const credential of credentials) await cdp.send('WebAuthn.addCredential', { authenticatorId: next, credential })
}

/** They registered a passkey some time ago. Registering counts as a step-up for that
 *  session, so they come back in another one, where replacing a key asks for a touch.
 *  They leave the page first: every response re-issues the session cookie
 *  (SESSION_SAVE_EVERY_REQUEST), so one still in flight would put the old session back. */
async function hasPasskeyFromBefore(page: Page, name: string) {
  await page.goto('/settings#passkeys')
  await page.getByTestId('passkey-register').click()
  await expect(page.getByTestId('passkey-add-another')).toBeVisible()
  await page.waitForLoadState('networkidle')
  await page.goto('about:blank')
  await page.context().addCookies([sessionCookie(`${name}-later`)])
  const status = await (await page.context().request.get('/api/passkeys/step-up')).json()
  expect(status.active, 'this visit must start with no step-up').toBe(false)
}

async function openNewKeyScreen(page: Page) {
  await page.goto('/settings#vault-key')
  await page.reload()
  await page.getByTestId('new-key-open').click()
  await expect(page.getByTestId('new-key-lead')).toContainText('does not need your current passphrase, Secret Key or passkey')
  await expect(page.getByTestId('new-key-no-vaults')).toBeVisible()
}

const sawUnlock = (page: Page) => page.evaluate(() => sessionStorage.getItem('saw-unlock') === '1')

/** A browser that remembers nothing: what a person has on a new device. */
async function freshLoad(page: Page) {
  await page.evaluate(() => localStorage.clear())
  await page.goto('/settings#vault-key')
  await page.reload()
}

/** The Emergency Kit stands in front of the rest of the card until it is saved. */
async function saveKit(page: Page): Promise<string> {
  const code = (await page.getByTestId('vault-secret-key').textContent()) ?? ''
  expect(code).toMatch(/^CV1-/)
  await page.getByTestId('vault-kit-saved').check()
  await page.getByTestId('vault-kit-done').click()
  return code
}

async function startPassphraseChange(page: Page) {
  await page.getByTestId('vault-key-add-passphrase').click()
  await page.getByLabel('New vault passphrase').fill(ANOTHER_PASSPHRASE)
  await page.getByLabel('Repeat the passphrase').fill(ANOTHER_PASSPHRASE)
  await page.getByTestId('vault-passphrase-save').click()
}

async function answerUnlock(page: Page, passphrase: string, secretKey: string) {
  await page.getByLabel('Vault passphrase', { exact: true }).fill(passphrase)
  await page.getByLabel('Secret Key').fill(secretKey)
  await page.getByTestId('vault-unlock-passphrase').click()
}

test('lost everything, with a passkey that said it has no PRF: a new passphrase and Secret Key that really open it', async ({ browser }) => {
  test.setTimeout(60_000)
  const page = await person(browser, 'vault-lost', { prf: false })
  await hasPasskeyFromBefore(page, 'vault-lost')
  await openNewKeyScreen(page)

  // That passkey cannot hold a key, so the screen asks for a new passphrase from the start.
  await expect(page.getByTestId('new-key-create')).toBeDisabled()
  await page.getByLabel('New vault passphrase').fill(NEW_PASSPHRASE)
  await page.getByLabel('Repeat the passphrase').fill(NEW_PASSPHRASE)
  // Replacing a key needs a fresh passkey touch: the server refuses, the app asks for the
  // touch by itself (the authenticator answers here) and retries.
  const refused = page.waitForResponse((r) => r.url().includes('/api/vault/keyset')
    && r.request().method() === 'PUT' && r.status() === 403)
  await page.getByTestId('new-key-create').click()
  expect((await (await refused).json()).error).toBe('step_up_required')

  const secretKey = await saveKit(page)
  expect(await sawUnlock(page)).toBe(false)

  // The new pair opens it in a browser that remembers nothing. Changing the passphrase
  // has to open the key first; a second Emergency Kit means it opened.
  await freshLoad(page)
  await startPassphraseChange(page)
  await answerUnlock(page, 'not the new passphrase', secretKey)
  await expect(page.getByTestId('vault-key-error')).toContainText(WAY_OUT)
  await page.getByTestId('vault-passphrase-save').click()
  await answerUnlock(page, NEW_PASSPHRASE, secretKey)
  await expect(page.getByTestId('vault-secret-key')).toBeVisible({ timeout: 20_000 })
  const next = (await page.getByTestId('vault-secret-key').textContent()) ?? ''
  expect(next).toMatch(/^CV1-/)
  expect(next).not.toBe(secretKey)
})

test('a passkey that turns out to give no PRF: the screen moves to a new passphrase, never to the old key', async ({ browser }) => {
  test.setTimeout(60_000)
  const page = await person(browser, 'vault-noprf', { prf: true })
  await hasPasskeyFromBefore(page, 'vault-noprf')
  await passkeyStopsGivingPrf(page)
  await openNewKeyScreen(page)
  await expect(page.getByTestId('new-key-passphrase')).toBeHidden()

  await page.getByTestId('new-key-create').click()
  await expect(page.getByTestId('new-key-error')).toContainText('can’t hold a vault key, so nothing was changed')
  await expect(page.getByTestId('new-key-create')).toBeDisabled()
  await page.getByLabel('New vault passphrase').fill(NEW_PASSPHRASE)
  await page.getByLabel('Repeat the passphrase').fill(NEW_PASSPHRASE)
  await page.getByTestId('new-key-create').click()

  await saveKit(page)
  await expect(page.getByTestId('vault-passphrase-row')).toContainText('✓')
  expect(await sawUnlock(page)).toBe(false)
})

test('a passkey with PRF: the new key is sealed to it, and that passkey opens it', async ({ browser }) => {
  test.setTimeout(60_000)
  const page = await person(browser, 'vault-prf', { prf: true })
  await hasPasskeyFromBefore(page, 'vault-prf')
  await openNewKeyScreen(page)
  await expect(page.getByTestId('new-key-passphrase')).toBeHidden()

  // One touch is both the step-up and the PRF output, so no second dialog.
  await page.getByTestId('new-key-create').click()
  await expect(page.getByTestId('new-key')).toBeHidden({ timeout: 20_000 })
  await expect(page.getByTestId('vault-passphrase-row')).toContainText('—')  // the old passphrase seal is gone
  await expect(page.locator('[data-testid^="vault-passkey-"]')).toContainText('✓')
  expect(await sawUnlock(page)).toBe(false)

  // The passkey alone opens it: changing the passphrase goes straight to the touch.
  await freshLoad(page)
  await startPassphraseChange(page)
  await expect(page.getByTestId('vault-secret-key')).toBeVisible({ timeout: 20_000 })
  await expect(page.getByTestId('vault-key-error')).toBeHidden()
})

test('holding a private conversation: the person chooses, and "I can’t unlock it" never asks for the old key', async ({ browser }) => {
  test.setTimeout(60_000)
  const page = await person(browser, 'vault-conversation', { prf: true })
  await hasPasskeyFromBefore(page, 'vault-conversation')
  await openNewKeyScreen(page)

  const asks = page.getByTestId('new-key-conversations')
  await expect(asks).toContainText('a private conversation with Hal')
  await expect(page.getByTestId('new-key-carry')).not.toBeChecked()
  await expect(page.getByTestId('new-key-without')).not.toBeChecked()
  await expect(page.getByTestId('new-key-create')).toBeDisabled()

  await page.getByTestId('new-key-without').check()
  await expect(page.getByTestId('new-key-without-note')).toBeVisible()
  await page.getByTestId('new-key-create').click()

  await expect(page.getByTestId('vault-key-notice')).toContainText('not carried over', { timeout: 20_000 })
  await expect(page.getByTestId('vault-key-notice')).toContainText('a private conversation with Hal')
  expect(await sawUnlock(page)).toBe(false)
})

test('the wrong passphrase points to the screen, and the link in the prompt opens it', async ({ browser }) => {
  const page = await person(browser, 'vault-wrong', { prf: null })
  await page.goto('/settings#vault-key')
  await startPassphraseChange(page)
  await answerUnlock(page, 'this is not the passphrase', WELL_FORMED_SECRET_KEY)
  await expect(page.getByTestId('vault-key-error')).toContainText(WAY_OUT, { timeout: 20_000 })
  await expect(page.getByTestId('vault-unlock')).toBeHidden()

  await page.getByTestId('vault-passphrase-save').click()
  const [screen] = await Promise.all([page.waitForEvent('popup'), page.getByTestId('vault-unlock-lost').click()])
  await expect(screen).toHaveURL(/\/settings#vault-key$/)
  await screen.getByTestId('new-key-open').click()
  await expect(screen.getByTestId('new-key-lead')).toBeVisible()
})
