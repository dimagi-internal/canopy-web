// @vitest-environment jsdom
import { StrictMode } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getShareState = vi.fn()
const putShare = vi.fn()
vi.mock('@/api/connections', async (orig) => ({
  ...(await orig<typeof import('@/api/connections')>()),
  getShareState: (id: string) => getShareState(id),
  putShare: (id: string, body: unknown) => putShare(id, body),
}))

const unlock = vi.fn()
vi.mock('@/vault/useVaultUnlock', () => ({ useVaultUnlock: () => ({ unlock, element: null }) }))

const openForOwner = vi.fn()
const trustFor = vi.fn()
const buildShare = vi.fn()
vi.mock('@/vault/connectionShare', async (orig) => ({
  ...(await orig<typeof import('@/vault/connectionShare')>()),
  openForOwner: (...a: unknown[]) => openForOwner(...a),
  trustFor: (...a: unknown[]) => trustFor(...a),
  buildShare: (...a: unknown[]) => buildShare(...a),
}))

import { ConnectedPage } from './ConnectedPage'
import { ApiError } from '@/api/problem'

const T = 'B'.repeat(43)
const SERVER = {
  available: true, thumbprint: 'T', public_jwk: {}, on: true, statements_enabled: true, provider_calls: true, reachable: true,
}
const EVA = { id: '12', slug: 'eva', name: 'Eva' }
const STATE = {
  id: 'c1', provider: 'salesforce', account_label: 'jj@dimagi.com', state: 'unshared', owner_agent: null, server: SERVER,
  agents: [EVA, { id: '40', slug: 'hal', name: 'Hal' }],
  candidates: [{ kind: 'device', target: T, agent_id: '12', runner_name: 'eva-cloud', mine: false, public_jwk: {} }],
}
const AGENT_STATE = { ...STATE, owner_agent: EVA }
const ME = { userId: '7', scalar: new Uint8Array(32) }
const READY_TRUST = {
  owner: { userId: '7', publicJwk: {}, jkt: 'j' },
  keys: new Map([[T, {}]]),
  assessments: new Map([['12', { endorsers: [], sealTo: [{ kind: 'device', target: T }] }]]),
}
const ASK_TRUST = {
  ...READY_TRUST,
  assessments: new Map([['12', { sealTo: [], endorsers: [{ by: '9', email: 'ada@dimagi.com', jkt: T, status: 'new', covers: [`device:${T}`] }] }]]),
}

function Where() {
  const l = useLocation()
  return <p data-testid="where">{l.pathname + l.search}</p>
}
const BACK = '?return_to=%2Fsettings%2Fconnections'
function open(query = BACK, strict = false) {
  const ui = (
    <MemoryRouter initialEntries={[`/w/dimagi/connections/c1/connected${query}`]}>
      <Routes>
        <Route path="/w/:workspace/connections/:id/connected" element={<><ConnectedPage /><Where /></>} />
      </Routes>
    </MemoryRouter>
  )
  return render(strict ? <StrictMode>{ui}</StrictMode> : ui)
}
const arrive = (strict = false) => open(`${BACK}&just_connected=1`, strict)
const href = (name: string | RegExp) => screen.getByRole('link', { name }).getAttribute('href')
const STORED = "Connected Eva as jj@dimagi.com and stored in Eva's vault. Eva can use it now."

beforeEach(() => {
  getShareState.mockResolvedValue(STATE)
  unlock.mockResolvedValue(ME)
  openForOwner.mockResolvedValue({ account: {}, statement: null })
  trustFor.mockResolvedValue(READY_TRUST)
  buildShare.mockResolvedValue({ share: { sig: 's' }, leftOut: [] })
  putShare.mockResolvedValue({})
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ConnectedPage, a person’s connection', () => {
  it('person sees Done and no Choose', async () => {
    getShareState.mockResolvedValue({ ...STATE, server: { ...SERVER, on: false, reachable: false } })
    open()
    expect(await screen.findByText('Connected as jj@dimagi.com and stored in your vault. Nothing uses it yet.')).toBeTruthy()
    expect(href('Done')).toBe('/settings/connections')
    expect(href('Share it with agents or your computers')).toBe(
      '/w/dimagi/connections/c1/share?return_to=%2Fsettings%2Fconnections',
    )
    expect(screen.queryByText(/choose/i)).toBeNull()
    expect(screen.queryByRole('link', { name: 'Choose' })).toBeNull()
  })

  it('a person’s connection with the key on keeps its wording', async () => {
    open()
    expect(await screen.findByText(
      'Connected as jj@dimagi.com and stored in your vault. Project screens and apps you use can use it now.',
    )).toBeTruthy()
    expect(href('Done')).toBe('/settings/connections')
    expect(href('Share it with agents or your computers')).toContain('/connections/c1/share')
  })

  it('a person’s connection makes no prompt, even just after the sign-in', async () => {
    arrive()
    await screen.findByText(/stored in your vault/)
    expect(unlock).not.toHaveBeenCalled()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('passes the agent it was asked for through to its Share link', async () => {
    open(`${BACK}&agent=eva`)
    await screen.findByText(/Connected as/)
    expect(href('Share it with agents or your computers')).toBe(
      '/w/dimagi/connections/c1/share?return_to=%2Fsettings%2Fconnections&agent=eva',
    )
  })

  it('a provider canopy-web never calls gets the off wording, even if the row says on', async () => {
    getShareState.mockResolvedValue({ ...STATE, provider: 'google', server: { ...SERVER, provider_calls: false } })
    open()
    expect(await screen.findByText('Connected as jj@dimagi.com and stored in your vault. Nothing uses it yet.')).toBeTruthy()
  })

  it("on but not reachable: says canopy-web couldn't be reached, never that screens can use it", async () => {
    getShareState.mockResolvedValue({ ...STATE, server: { ...SERVER, on: true, reachable: false } })
    open()
    expect(await screen.findByText(
      "Connected as jj@dimagi.com. canopy-web couldn't be reached to turn on project screens and apps.",
    )).toBeTruthy()
    expect(screen.queryByText(/can use it now/)).toBeNull()
    expect(screen.queryByText(/choose/i)).toBeNull()
    expect(href('Done')).toBe('/settings/connections')
  })

  it("the key can't be read right now: says so, with a person's links", async () => {
    getShareState.mockResolvedValue({ ...STATE, server: { ...SERVER, on: true, reachable: null } })
    open()
    expect(await screen.findByText(
      "Connected as jj@dimagi.com. canopy-web can't be reached right now, so project screens and apps may not work for a moment.",
    )).toBeTruthy()
    expect(href('Share it with agents or your computers')).toContain('/connections/c1/share')
    expect(href('Done')).toBe('/settings/connections')
  })

  it('an empty account name falls back to the account id, never "Connected as ."', async () => {
    getShareState.mockResolvedValue({ ...STATE, account_label: '', account_id: '00Dxx/005yy' })
    open()
    expect(await screen.findByText(/^Connected as 00Dxx\/005yy and stored/)).toBeTruthy()
  })

  it('an unsafe return_to falls back to Settings for Done', async () => {
    open('?return_to=%2F%2Fevil.test')
    await screen.findByText(/Connected as/)
    expect(href('Done')).toBe('/settings')
  })

  it("when the connection can't be read here (someone without the key), says Connected with Done", async () => {
    getShareState.mockRejectedValue(new ApiError(403, 'x', { status: 403, code: 'not_the_owner_key', error: 'not_the_owner_key' }))
    arrive()
    expect(await screen.findByText('Connected.')).toBeTruthy()
    expect(href('Done')).toBe('/settings/connections')
    expect(unlock).not.toHaveBeenCalled()
  })
})

describe('ConnectedPage, an agent’s connection', () => {
  it('names the machines before the store and after it, with no prompt until the button', async () => {
    getShareState.mockResolvedValue(AGENT_STATE)
    open()
    expect(await screen.findByText("It goes to Eva's approved machines: eva-cloud.")).toBeTruthy()
    expect(unlock).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Store for Eva' }))
    expect(await screen.findByText("Stored on Eva's machines: eva-cloud.")).toBeTruthy()
    expect(unlock).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('link', { name: /Share/ })).toBeNull()
  })

  beforeEach(() => getShareState.mockResolvedValue(AGENT_STATE))

  it('agent stores automatically on arrival with no click', async () => {
    arrive()
    expect(await screen.findByText(STORED)).toBeTruthy()
    expect(unlock).toHaveBeenCalledTimes(1)
    expect(putShare).toHaveBeenCalledWith('c1', { sig: 's' })
    expect(href('Done')).toBe('/settings/connections')
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('shows what it is doing while it stores', async () => {
    unlock.mockReturnValue(new Promise(() => {}))
    arrive()
    expect(await screen.findByText("Connected Eva as jj@dimagi.com. Storing it in Eva's vault…")).toBeTruthy()
  })

  it('shares with only the agent and no computers, canopy-web only when the row has it on', async () => {
    arrive()
    await screen.findByText(STORED)
    expect(buildShare.mock.calls[0][2]).toEqual({ agents: ['12'], myComputers: [], canopyWeb: true })
    cleanup()
    vi.clearAllMocks()
    getShareState.mockResolvedValue({ ...AGENT_STATE, server: { ...SERVER, on: false } })
    unlock.mockResolvedValue(ME)
    openForOwner.mockResolvedValue({ account: {}, statement: null })
    trustFor.mockResolvedValue(READY_TRUST)
    buildShare.mockResolvedValue({ share: { sig: 's' }, leftOut: [] })
    arrive()
    await screen.findByText(STORED)
    expect(buildShare.mock.calls[0][2]).toEqual({ agents: ['12'], myComputers: [], canopyWeb: false })
  })

  it('does it only once', async () => {
    arrive(true)
    await screen.findByText(STORED)
    expect(unlock).toHaveBeenCalledTimes(1)
    expect(putShare).toHaveBeenCalledTimes(1)
  })

  it('takes just_connected off the address once read', async () => {
    arrive()
    await screen.findByText(STORED)
    expect(screen.getByTestId('where').textContent).not.toContain('just_connected')
    expect(screen.getByTestId('where').textContent).toContain('return_to=%2Fsettings%2Fconnections')
  })

  it("a reload without just_connected doesn't prompt", async () => {
    open()
    expect(await screen.findByRole('button', { name: "Store for Eva" })).toBeTruthy()
    expect(unlock).not.toHaveBeenCalled()
    expect(href('Later')).toBe('/settings/connections')
  })

  it("a refused or dismissed passkey shows Store for Eva, and the button works", async () => {
    unlock.mockRejectedValueOnce(new DOMException('not allowed', 'NotAllowedError'))
    arrive()
    const button = await screen.findByRole('button', { name: "Store for Eva" })
    expect(screen.getByText('Connected Eva as jj@dimagi.com.')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(href('Later')).toBe('/settings/connections')
    expect(putShare).not.toHaveBeenCalled()
    fireEvent.click(button)
    expect(await screen.findByText(STORED)).toBeTruthy()
    expect(unlock).toHaveBeenCalledTimes(2)
  })

  it('a failure shows its sentence and the button to try again', async () => {
    putShare.mockRejectedValueOnce(new Error('The server said no.'))
    arrive()
    expect((await screen.findByText('The server said no.'))).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: "Store for Eva" }))
    expect(await screen.findByText(STORED)).toBeTruthy()
  })

  it('an already unlocked vault makes no button at all', async () => {
    arrive()
    await screen.findByText(STORED)
    expect(screen.queryByRole('button', { name: /Store for/ })).toBeNull()
    expect(screen.queryByText(/passkey/i)).toBeNull()
  })

  it('a trust question links to Share rather than guessing', async () => {
    trustFor.mockResolvedValue(ASK_TRUST)
    arrive()
    expect(await screen.findByText(/needs a decision from you/)).toBeTruthy()
    expect(href('Open the Share screen')).toBe('/w/dimagi/connections/c1/share?return_to=%2Fsettings%2Fconnections')
    expect(buildShare).not.toHaveBeenCalled()
    expect(putShare).not.toHaveBeenCalled()
  })

  it('no machine to seal to links to Share rather than guessing', async () => {
    getShareState.mockResolvedValue({ ...AGENT_STATE, candidates: [] })
    trustFor.mockResolvedValue({ ...READY_TRUST, assessments: new Map([['12', { endorsers: [], sealTo: [] }]]) })
    arrive()
    expect(await screen.findByText("None of Eva's machines are ready yet. Try again once one is online.")).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
    expect(href('Open the Share screen')).toContain('/connections/c1/share')
    expect(buildShare).not.toHaveBeenCalled()
  })

  it('an agent row that is already shared says it can use it', async () => {
    getShareState.mockResolvedValue({ ...AGENT_STATE, state: 'active' })
    arrive()
    expect(await screen.findByText('Eva can use it.')).toBeTruthy()
    expect(href('Done')).toBe('/settings/connections')
    expect(unlock).not.toHaveBeenCalled()
  })

  it("says canopy-web couldn't be reached, without the word choose", async () => {
    getShareState.mockResolvedValue({ ...AGENT_STATE, state: 'active', server: { ...SERVER, on: true, reachable: false } })
    open()
    expect(await screen.findByText(/canopy-web couldn't be reached to turn on project screens and apps\./)).toBeTruthy()
    expect(screen.queryByText(/choose/i)).toBeNull()
  })

  it('a reconnect keeps what was signed before: the same agents and canopy-web', async () => {
    openForOwner.mockResolvedValue({ account: {}, statement: { agents: ['12', '40'], my_computers: [], canopy_web: 'T' } })
    arrive()
    await screen.findByText(STORED)
    expect(buildShare.mock.calls[0][2]).toEqual({ agents: ['12', '40'], myComputers: [], canopyWeb: true })
  })

  it('an earlier choice that is no longer choosable goes to Share instead of narrowing it', async () => {
    openForOwner.mockResolvedValue({ account: {}, statement: { agents: ['99'], my_computers: [], canopy_web: 'T' } })
    arrive()
    expect(await screen.findByText(/needs a decision from you/)).toBeTruthy()
    expect(buildShare).not.toHaveBeenCalled()
    expect(href('Open the Share screen')).toContain('/connections/c1/share')
  })

  it('project screens on but not nameable goes to Share, never turning them off silently', async () => {
    getShareState.mockResolvedValue({ ...AGENT_STATE, server: { ...SERVER, statements_enabled: false } })
    arrive()
    expect(await screen.findByText(/needs a decision from you/)).toBeTruthy()
    expect(buildShare).not.toHaveBeenCalled()
  })

  it('a row with project screens off still stores with no key at all', async () => {
    getShareState.mockResolvedValue({ ...AGENT_STATE, server: { available: false, provider_calls: false, on: false, reachable: false } })
    arrive()
    await screen.findByText(/stored in Eva's vault/)
    expect(buildShare.mock.calls[0][2]).toEqual({ agents: ['12'], myComputers: [], canopyWeb: false })
  })

  it("a changed canopy-web key goes to Share rather than being signed", async () => {
    openForOwner.mockResolvedValue({ account: {}, statement: { agents: ['12'], my_computers: [], canopy_web: 'OLD' } })
    arrive()
    expect(await screen.findByText(/needs a decision from you/)).toBeTruthy()
    expect(buildShare).not.toHaveBeenCalled()
  })

  it('a reconnect that would sign something different goes to Share', async () => {
    // Before: no canopy-web. Now the row has it on, so the share would differ from what was signed.
    openForOwner.mockResolvedValue({ account: {}, statement: { agents: ['12'], my_computers: [] } })
    arrive()
    expect(await screen.findByText(/needs a decision from you/)).toBeTruthy()
    expect(buildShare).not.toHaveBeenCalled()
  })

  it('the arrival try never opens the passphrase dialog; the button may', async () => {
    unlock.mockRejectedValueOnce(new DOMException('no', 'NotAllowedError'))
    arrive()
    fireEvent.click(await screen.findByRole('button', { name: "Store for Eva" }))
    await screen.findByText(STORED)
    expect(unlock.mock.calls[0][0]).toEqual({ passkeyOnly: true })
    expect(unlock.mock.calls[1][0]).toEqual({ passkeyOnly: false })
  })

  it('the button reads the connection again before it tries', async () => {
    putShare.mockRejectedValueOnce(new Error('The server said no.'))
    arrive()
    await screen.findByText('The server said no.')
    const before = getShareState.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: "Store for Eva" }))
    await screen.findByText(STORED)
    expect(getShareState.mock.calls.length).toBeGreaterThan(before)
  })

  it('a stale statement reads again and tries once more by itself', async () => {
    putShare.mockRejectedValueOnce(new ApiError(409, 'x', { status: 409, code: 'statement_stale', error: 'statement_stale' }))
    arrive()
    await screen.findByText(STORED)
    expect(putShare).toHaveBeenCalledTimes(2)
    expect(unlock).toHaveBeenCalledTimes(1)
  })

  it('does not say canopy-web could not be reached once the share sealed to it', async () => {
    getShareState.mockResolvedValueOnce({ ...AGENT_STATE, server: { ...SERVER, reachable: false } })
    getShareState.mockResolvedValue(AGENT_STATE)
    arrive()
    expect(await screen.findByText(STORED)).toBeTruthy()
    expect(screen.queryByText(/couldn't be reached/)).toBeNull()
  })
})

describe('ConnectedPage, a person reconnecting', () => {
  it('says what stopped, and offers Share again with Later', async () => {
    getShareState.mockResolvedValue({ ...STATE, statement: { agents: ['12'], my_computers: ['x'], version: 2 } })
    open()
    expect(await screen.findByText(
      'Connected as jj@dimagi.com and stored in your vault. The agents or computers that used it need it shared again.',
    )).toBeTruthy()
    expect(href('Share again')).toBe('/w/dimagi/connections/c1/share?return_to=%2Fsettings%2Fconnections')
    expect(href('Later')).toBe('/settings/connections')
    expect(screen.queryByRole('link', { name: 'Done' })).toBeNull()
  })
})
