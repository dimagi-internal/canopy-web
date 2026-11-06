import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'
import { expect, test, type APIRequestContext, type Browser, type CDPSession, type Page, type Route } from '@playwright/test'

// An admin connects an AGENT to Salesforce from the agent's External accounts tab, as a person
// meets it: the Connect tap opens their vault key (a real passkey ceremony against a Chromium
// virtual authenticator with PRF), the real start view sends the browser to Salesforce, the real
// callback seals the sign-in to the admin's key, and the Connected page opens that key again and
// stores the sign-in for the agent's machine in the browser's real crypto.
//
// Salesforce is the only stand-in. Its sign-in page is `page.route` below, which sends the browser
// straight back to the callback with `code=e2e-<agent>`; the server's code exchange and identity
// lookup are answered in-process by `fake_salesforce.py` (the e2e API's entry point). The account
// signed in is `<agent>@dimagi-ai.com`.
//
// seed_connect.py gives each scenario its own admin and agent (Eva, Iris, Bea), each agent with one
// cloud machine. An admin who can finish a connect starts with a vault key sealed by a passphrase
// and Secret Key, the machine vouched for by that key; the test adds a PRF passkey to it the way a
// person does under Settings.

const here = path.dirname(fileURLToPath(import.meta.url))
const PASSPHRASE = 'connect e2e passphrase'
const START = '/auth/connections/salesforce/dimagi/start/'
const ARRIVAL_REFUSED = "Your browser didn't open the passkey prompt by itself (some phones and password managers only do it after a tap). Tap Try again."

const readAuth = (file: string) => fs.readFileSync(path.join(here, '.auth', `${file}.txt`), 'utf8').trim()
const sessionCookie = (file: string) => ({
  name: 'sessionid', value: readAuth(file),
  domain: 'localhost', path: '/', expires: Math.floor(Date.now() / 1000) + 86400,
  httpOnly: true, secure: false, sameSite: 'Lax' as const,
})

const authenticators = new WeakMap<Page, { cdp: CDPSession; id: string }>()

async function addAuthenticator(cdp: CDPSession, prf: boolean): Promise<string> {
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', { options: {
    protocol: 'ctap2', ctap2Version: 'ctap2_1', transport: 'internal', hasResidentKey: true,
    hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true, hasPrf: prf,
  } })
  return authenticatorId
}

async function admin(browser: Browser, name: string): Promise<Page> {
  const context = await browser.newContext({
    baseURL: 'http://localhost:3000',
    storageState: { cookies: [sessionCookie(name)], origins: [] },
  })
  const page = await context.newPage()
  const cdp = await context.newCDPSession(page)
  await cdp.send('WebAuthn.enable')
  authenticators.set(page, { cdp, id: await addAuthenticator(cdp, true) })
  return page
}

/** The same passkey, now giving no PRF output (as vault-new-key.spec.ts does it). */
async function passkeyStopsGivingPrf(page: Page) {
  const { cdp, id } = authenticators.get(page)!
  const { credentials } = await cdp.send('WebAuthn.getCredentials', { authenticatorId: id })
  await cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId: id })
  const next = await addAuthenticator(cdp, false)
  for (const credential of credentials) await cdp.send('WebAuthn.addCredential', { authenticatorId: next, credential })
  authenticators.set(page, { cdp, id: next })
}

async function registerPasskey(page: Page) {
  await page.goto('/settings#passkeys')
  await page.getByTestId('passkey-register').click()
  await expect(page.getByTestId('passkey-add-another')).toBeVisible()
  await page.waitForLoadState('networkidle')
}

/** Settings → Security → "Add a passkey to it": the passphrase and Secret Key open the key, then
 *  the passkey's touch seals it. From here the passkey alone opens it. */
async function passkeyHoldsVaultKey(page: Page, name: string) {
  await registerPasskey(page)
  await page.goto('/settings#vault-key')
  await page.reload()
  await page.getByTestId('vault-key-add-passkey').click()
  await page.getByLabel('Vault passphrase', { exact: true }).fill(PASSPHRASE)
  await page.getByLabel('Secret Key').fill(readAuth(`${name}-secret-key`))
  await page.getByTestId('vault-unlock-passphrase').click()
  await expect(page.locator('[data-testid^="vault-passkey-"]')).toContainText('✓', { timeout: 20_000 })
}

/** Salesforce's sign-in page: it signs in the agent's own account and sends the browser back to
 *  canopy-web's callback, as Salesforce does after the admin types the agent's credentials.
 *  A route never sees a request the browser makes by following a redirect, so it is the start's
 *  answer that is caught: the real start view runs, and its redirect to Salesforce is answered
 *  with Salesforce's redirect back. */
async function salesforceSignsIn(page: Page, agent: string) {
  await page.route(`**${START}`, async (route: Route) => {
    const started = await route.fetch({ maxRedirects: 0 })
    expect(started.status(), await started.text()).toBe(302)
    const asked = new URL(started.headers()['location'] ?? '')
    expect(asked.origin).toBe('https://login.salesforce.com')
    expect(asked.pathname).toBe('/services/oauth2/authorize')
    expect(asked.searchParams.get('client_id')).toBe('3MVG-e2e-client')
    // An agent's connect asks Salesforce for a fresh login, never the admin's own session.
    expect(asked.searchParams.get('prompt')).toBe('login consent')
    expect(asked.searchParams.get('code_challenge_method')).toBe('S256')
    const callback = new URL(asked.searchParams.get('redirect_uri') ?? '')
    expect(callback.pathname).toBe('/auth/connections/salesforce/dimagi/callback/')
    const back = new URL(callback.pathname, 'http://localhost:3000')
    back.searchParams.set('code', `e2e-${agent}`)
    back.searchParams.set('state', asked.searchParams.get('state') ?? '')
    await route.fulfill({ status: 302, headers: { location: back.toString() } })
  })
}

async function openAccounts(page: Page, agent: string) {
  await page.goto(`/w/dimagi/agents/${agent}/settings/accounts`)
  await expect(page.getByTestId('agent-connection-provider-salesforce')).toContainText('Not connected')
}

type Event = { kind: string; payload: { agent?: string; step?: string; trigger?: string; outcome?: string; error?: string } }

/** The workspace's event log, read as its owner (the e2e user): the admin connecting is an editor. */
async function connectEvents(request: APIRequestContext, agent: string): Promise<Event[]> {
  const res = await request.get('/api/events/?source=connections.connect&limit=200')
  expect(res.ok()).toBe(true)
  return ((await res.json()).items as Event[]).filter((e) => e.payload.agent === agent)
}

const has = (events: Event[], want: Partial<Event['payload']> & { kind: string }) =>
  events.some((e) => e.kind === want.kind && Object.entries(want).every(([k, v]) => k === 'kind'
    || e.payload[k as keyof Event['payload']] === v))

test('connecting Eva: the tap opens the vault key, Salesforce signs her in, and the Connected page stores it for her machine', async ({ browser, request }) => {
  test.setTimeout(90_000)
  const page = await admin(browser, 'connect-ok')
  await passkeyHoldsVaultKey(page, 'connect-ok')
  await salesforceSignsIn(page, 'eva')
  await openAccounts(page, 'eva')

  await page.getByRole('button', { name: 'Connect Salesforce' }).click()
  await page.waitForURL(/\/w\/dimagi\/connections\/[^/]+\/connected/)
  const connected = page.getByTestId('connected')
  await expect(connected.getByRole('status')).toHaveText(
    'Connected Eva as eva@dimagi-ai.com. Eva can use it now.', { timeout: 30_000 })
  await expect(connected).toContainText("It's on Eva's machines: eva-cloud.")
  await expect(connected.getByRole('heading')).toHaveText('Connected')
  await expect(page.getByRole('alert')).toHaveCount(0)
  // `just_connected` is read once and taken off the address, so a reload never prompts.
  expect(new URL(page.url()).searchParams.has('just_connected')).toBe(false)

  // Eva's connections now list it, in force.
  const list = await (await page.context().request.get('/api/agents/eva/connections')).json()
  const mine = (list.connections as { provider: string; account_label: string; state: string; usable: boolean }[])
    .filter((c) => c.provider === 'salesforce')
  expect(mine).toHaveLength(1)
  expect(mine[0]).toMatchObject({ account_label: 'eva@dimagi-ai.com', usable: true })
  expect(mine[0].state).not.toBe('unshared')

  await expect.poll(async () => {
    const events = await connectEvents(request, 'eva')
    return has(events, { kind: 'ok', step: 'preflight', trigger: 'tap' })
      && has(events, { kind: 'ok', step: 'store', trigger: 'arrival' })
  }, { timeout: 10_000 }).toBe(true)

  // Back on the tab, Eva's row is connected.
  await page.getByRole('link', { name: 'Done' }).click()
  await expect(page.getByTestId('agent-connection-provider-salesforce')).toHaveCount(0)
  await expect(page.getByText('eva@dimagi-ai.com').first()).toBeVisible()
})

test("a passkey that gives no PRF: the tap stops before Salesforce and says why", async ({ browser, request }) => {
  test.setTimeout(90_000)
  const page = await admin(browser, 'connect-noprf')
  // Their only way into the vault key is that passkey: a new key, held by it alone.
  await registerPasskey(page)
  await page.goto('about:blank')
  await page.context().addCookies([sessionCookie('connect-noprf-later')])
  await page.goto('/settings#vault-key')
  await page.reload()
  await page.getByTestId('new-key-open').click()
  await page.getByTestId('new-key-create').click()
  await expect(page.getByTestId('new-key')).toBeHidden({ timeout: 20_000 })
  await expect(page.getByTestId('vault-passphrase-row')).toContainText('—')
  await passkeyStopsGivingPrf(page)

  const started: string[] = []
  page.on('request', (r) => { if (r.url().includes('/auth/connections/')) started.push(r.url()) })
  await openAccounts(page, 'bea')
  await page.getByRole('button', { name: 'Connect Salesforce' }).click()

  const alert = page.getByTestId('agent-connection-provider-salesforce').getByRole('alert')
  await expect(alert).toHaveText(/^Not connected yet: /, { timeout: 20_000 })
  await expect(alert).toContainText('Your passkey did not open your vault key.')
  await expect(page.getByRole('button', { name: 'Connect Salesforce' })).toBeEnabled()
  expect(started, 'the browser never left for the provider').toEqual([])
  expect(new URL(page.url()).pathname).toBe('/w/dimagi/agents/bea/settings/accounts')

  await expect.poll(async () => has(await connectEvents(request, 'bea'),
    { kind: 'failed', step: 'preflight', trigger: 'tap' }), { timeout: 10_000 }).toBe(true)
  // Nothing was half-made.
  const list = await (await page.context().request.get('/api/agents/bea/connections')).json()
  expect(list.connections).toEqual([])
})

test('the browser will not open the passkey on arrival: the page says so, and Try again connects Iris', async ({ browser, request }) => {
  test.setTimeout(90_000)
  const page = await admin(browser, 'connect-retry')
  await passkeyHoldsVaultKey(page, 'connect-retry')
  // What some phones and password managers do: no passkey prompt without a tap on that page. The
  // Connected page's own try is refused once; the Try again tap goes to the real authenticator.
  await page.addInitScript(() => {
    if (!/\/connections\/[^/]+\/connected/.test(location.pathname)) return
    const real = navigator.credentials.get.bind(navigator.credentials)
    let refused = false
    navigator.credentials.get = (options?: CredentialRequestOptions) => {
      if (refused) return real(options)
      refused = true
      return Promise.reject(new DOMException('The operation either timed out or was not allowed.', 'NotAllowedError'))
    }
  })
  await salesforceSignsIn(page, 'iris')
  await openAccounts(page, 'iris')

  await page.getByRole('button', { name: 'Connect Salesforce' }).click()
  await page.waitForURL(/\/w\/dimagi\/connections\/[^/]+\/connected/)
  const connected = page.getByTestId('connected')
  await expect(connected.getByRole('heading')).toHaveText('Not connected', { timeout: 30_000 })
  await expect(connected.getByRole('status')).toHaveText(
    "Connecting Iris didn't finish, so Iris can't use iris@dimagi-ai.com.")
  await expect(connected.getByRole('alert')).toHaveText(ARRIVAL_REFUSED)
  await expect.poll(async () => has(await connectEvents(request, 'iris'),
    { kind: 'cancelled', step: 'unlock', trigger: 'arrival' }), { timeout: 10_000 }).toBe(true)
  // Unfinished, so not listed: leaving now would leave Iris simply not connected.
  const before = await (await page.context().request.get('/api/agents/iris/connections')).json()
  expect(before.connections).toEqual([])

  await connected.getByRole('button', { name: 'Try again' }).click()
  await expect(connected.getByRole('status')).toHaveText(
    'Connected Iris as iris@dimagi-ai.com. Iris can use it now.', { timeout: 30_000 })
  await expect(connected.getByRole('heading')).toHaveText('Connected')
  await expect(connected.getByRole('alert')).toHaveCount(0)
  await expect.poll(async () => has(await connectEvents(request, 'iris'),
    { kind: 'ok', step: 'store', trigger: 'tap' }), { timeout: 10_000 }).toBe(true)
  const after = await (await page.context().request.get('/api/agents/iris/connections')).json()
  expect((after.connections as { account_label: string }[]).map((c) => c.account_label)).toEqual(['iris@dimagi-ai.com'])
})
