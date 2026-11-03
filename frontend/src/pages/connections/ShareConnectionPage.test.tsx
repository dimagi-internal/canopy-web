// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
const leftOut = vi.fn()
const changedEndorser = vi.fn()
vi.mock('@/vault/connectionShare', async (orig) => ({
  ...(await orig<typeof import('@/vault/connectionShare')>()),
  openForOwner: (...a: unknown[]) => openForOwner(...a),
  trustFor: (...a: unknown[]) => trustFor(...a),
  buildShare: (...a: unknown[]) => buildShare(...a),
  leftOut: (...a: unknown[]) => leftOut(...a),
  changedEndorser: (...a: unknown[]) => changedEndorser(...a),
}))

const pinPerson = vi.fn()
vi.mock('@/vault/trust', async (orig) => ({
  ...(await orig<typeof import('@/vault/trust')>()),
  pinPerson: (...a: unknown[]) => pinPerson(...a),
}))

import { ShareConnectionPage, safeReturnTo } from './ShareConnectionPage'
import { ShareRefused } from '@/vault/connectionShare'
import { ApiError } from '@/api/problem'

const T1 = 'A'.repeat(43)
const T2 = 'B'.repeat(43)
const ME = { userId: '7', scalar: new Uint8Array(32) }
const TRUST = { owner: {}, keys: new Map(), assessments: new Map() }
const STATE = {
  id: 'c1', binding: 'connection:c1', provider: 'salesforce', account_id: '00D/005', account_label: 'Server Label',
  state: 'unshared', sealed_sig: { by: {} }, statement: null, statement_sig: '',
  owner_agent: null,
  agents: [{ id: '12', slug: 'eva', name: 'Eva' }, { id: '40', slug: 'hal', name: 'Hal' }],
  candidates: [
    { kind: 'member', target: '7', agent_id: '', runner_name: '', mine: true, public_jwk: {} },
    { kind: 'device', target: T1, agent_id: '', runner_name: 'jj-laptop', mine: true, public_jwk: {} },
    { kind: 'device', target: T2, agent_id: '12', runner_name: 'eva-cloud', mine: false, public_jwk: {} },
  ],
}
const ACCOUNT = { account_id: '00Dxx/005yy', instance_url: 'https://acme.my.salesforce.com', provider: 'salesforce' }

function Where() {
  const l = useLocation()
  return <p data-testid="where">{l.pathname + l.search}</p>
}
function open(query = '?return_to=%2Fsettings%2Fconnections') {
  return render(
    <MemoryRouter initialEntries={[`/w/dimagi/connections/c1/share${query}`]}>
      <Routes>
        <Route path="/w/:workspace/connections/:id/share" element={<ShareConnectionPage />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
}
const unlocked = async () => {
  open()
  fireEvent.click(await screen.findByRole('button', { name: /unlock/i }))
  await screen.findByText(/Connected as/)
}

beforeEach(() => {
  getShareState.mockResolvedValue(STATE)
  unlock.mockResolvedValue(ME)
  openForOwner.mockResolvedValue({ account: ACCOUNT, statement: null })
  trustFor.mockResolvedValue(TRUST)
  leftOut.mockReturnValue([])
  changedEndorser.mockReturnValue(undefined)
  buildShare.mockResolvedValue({ share: { sig: 's' }, leftOut: [] })
  putShare.mockResolvedValue({})
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('ShareConnectionPage', () => {
  it('shows the account from the opened envelope before sharing', async () => {
    await unlocked()
    expect(screen.getByText('Connected as Server Label')).toBeTruthy()
    expect(screen.getByText('Account ID: 00Dxx/005yy')).toBeTruthy()
    expect(openForOwner).toHaveBeenCalledWith(STATE, ME)
    expect(unlock).toHaveBeenCalledTimes(1)
    // machines by name and fingerprint; agents by name
    expect(screen.getByText('jj-laptop')).toBeTruthy()
    expect(screen.getByText('AAAA AAAA AAAA AAAA AAAA AAAA AAAA AAAA')).toBeTruthy()
    expect(screen.getByLabelText('Eva')).toBeTruthy()
  })

  it('shows only the checked id as the main line when the label is empty', async () => {
    getShareState.mockResolvedValue({ ...STATE, account_label: '' })
    await unlocked()
    expect(screen.getByText('Connected as 00Dxx/005yy')).toBeTruthy()
    expect(screen.queryByText(/Account ID:/)).toBeNull()
  })

  it('tells a person with no enrolled computers how to enrol one', async () => {
    getShareState.mockResolvedValue({ ...STATE, candidates: STATE.candidates.filter((c: { kind: string }) => c.kind !== 'device') })
    await unlocked()
    expect(screen.getByText('No enrolled computers. Run: canopy vault device --enroll')).toBeTruthy()
  })

  it('withdraws the last grant: everyone unticked goes through to the PUT, once it was shared', async () => {
    const statement = { agents: ['12'], my_computers: [] }
    openForOwner.mockResolvedValue({ account: ACCOUNT, statement })
    getShareState.mockResolvedValue({ ...STATE, state: 'active', statement })
    await unlocked()
    fireEvent.click(screen.getByLabelText('Eva'))
    const share = screen.getByRole('button', { name: 'Share' }) as HTMLButtonElement
    expect(share.disabled).toBe(false)
    fireEvent.click(share)
    await waitFor(() => expect(putShare).toHaveBeenCalledWith('c1', { sig: 's' }))
    expect(buildShare.mock.calls[0][2]).toEqual({ agents: [], myComputers: [], canopyWeb: false })
  })

  it('offers to trust a person vouching for a chosen machine, by name and fingerprint, and reads again', async () => {
    const ada = { by: '9', email: 'ada@dimagi.com', jkt: T1, status: 'new', covers: [`device:${T2}`] }
    const verdict = (status: string) => ({ endorsers: [{ ...ada, status }], sealTo: status === 'new' ? [] : [{ kind: 'device', target: T2 }] })
    trustFor.mockResolvedValueOnce({ ...TRUST, assessments: new Map([['12', verdict('new')]]) })
    trustFor.mockResolvedValueOnce({ ...TRUST, assessments: new Map([['12', verdict('pinned')]]) })
    await unlocked()
    expect(screen.queryByTestId('vouchers')).toBeNull() // nothing chosen yet
    fireEvent.click(screen.getByLabelText('Eva'))
    const offer = await screen.findByRole('button', { name: 'Trust ada@dimagi.com’s vouching' })
    expect(screen.getByTestId('vouchers').textContent).toContain('AAAA AAAA')
    fireEvent.click(offer)
    await waitFor(() => expect(screen.queryByTestId('vouchers')).toBeNull())
    expect(trustFor.mock.calls[1][2].accept.has('9')).toBe(true)
    expect(pinPerson).toHaveBeenCalledWith('9', T1, 'ada@dimagi.com')
  })

  it('sends a signed statement and a re-sealed envelope with the chosen agents', async () => {
    await unlocked()
    fireEvent.click(screen.getByLabelText('Eva'))
    fireEvent.click(screen.getByLabelText(/jj-laptop/))
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    await waitFor(() => expect(putShare).toHaveBeenCalledWith('c1', { sig: 's' }))
    expect(buildShare).toHaveBeenCalledWith(STATE, ME, { agents: ['12'], myComputers: [T1], canopyWeb: false }, TRUST)
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/settings/connections'))
  })

  it('disables Share until a person-owned connection has someone chosen', async () => {
    await unlocked()
    expect((screen.getByRole('button', { name: 'Share' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByLabelText('Hal'))
    expect((screen.getByRole('button', { name: 'Share' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('pre-checks and fixes the owning agent of an agent-owned connection', async () => {
    getShareState.mockResolvedValue({ ...STATE, owner_agent: { id: '12', slug: 'eva', name: 'Eva' } })
    await unlocked()
    const box = screen.getByLabelText('Eva') as HTMLInputElement
    expect(box.checked).toBe(true)
    expect(box.disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    await waitFor(() => expect(buildShare).toHaveBeenCalled())
    expect(buildShare.mock.calls[0][2]).toEqual({ agents: ['12'], myComputers: [], canopyWeb: false })
  })

  it('Not now leaves it unshared and says so', async () => {
    await unlocked()
    expect(screen.getByText('Not shared yet: no agent or computer can use it.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }))
    expect(putShare).not.toHaveBeenCalled()
    expect(buildShare).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/settings/connections'))
  })

  it('names a machine left out and why', async () => {
    leftOut.mockReturnValue([{ target: T2, runner_name: 'eva-cloud', agent_id: '12', why: 'not vouched for by anyone you trust' }])
    await unlocked()
    fireEvent.click(screen.getByLabelText('Eva'))
    expect(screen.getByTestId('left-out').textContent).toContain('eva-cloud')
    expect(screen.getByTestId('left-out').textContent).toContain('not vouched for by anyone you trust')
  })

  it('resumes sharing from Settings: an unshared row opens with what was last signed', async () => {
    openForOwner.mockResolvedValue({
      account: ACCOUNT,
      statement: { agents: ['40'], my_computers: [T1], endorsers: [], version: 2 },
    })
    await unlocked()
    expect((screen.getByLabelText('Hal') as HTMLInputElement).checked).toBe(true)
    expect((screen.getByLabelText('Eva') as HTMLInputElement).checked).toBe(false)
    expect((screen.getByLabelText(/jj-laptop/) as HTMLInputElement).checked).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    await waitFor(() => expect(buildShare.mock.calls[0][2]).toEqual({ agents: ['40'], myComputers: [T1], canopyWeb: false }))
    expect(screen.queryByText(/was reconnected/)).toBeNull()
  })

  it('warns, without blocking, when a reconnected connection already had a statement', async () => {
    getShareState.mockResolvedValue({ ...STATE, sealed_sig: null })
    openForOwner.mockResolvedValue({
      account: ACCOUNT, statement: { agents: ['40'], my_computers: [], endorsers: [], version: 2 },
    })
    await unlocked()
    expect(screen.getByText('This connection was reconnected. If you did not just do that, stop and tell an admin.')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Share' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('does not warn on a first connect (no statement yet)', async () => {
    getShareState.mockResolvedValue({ ...STATE, sealed_sig: null })
    await unlocked()
    expect(screen.queryByText(/was reconnected/)).toBeNull()
  })

  it.each(['bad-writer', 'bad-mac', 'bad-statement-sig', 'something-else'])(
    'says it cannot be opened safely and offers Reconnect, never only the code (%s)', async (code) => {
      openForOwner.mockRejectedValue(new ShareRefused(code, 'detail'))
      open()
      fireEvent.click(await screen.findByRole('button', { name: /unlock/i }))
      expect(await screen.findByText("This connection can't be opened safely. Reconnect it.")).toBeTruthy()
      const link = screen.getByRole('link', { name: 'Reconnect' }) as HTMLAnchorElement
      expect(link.getAttribute('href')).toContain('/auth/connections/salesforce/dimagi/start/')
      expect(screen.queryByText(code)).toBeNull()
      expect(screen.queryByRole('button', { name: 'Share' })).toBeNull()
    })

  it('says a person whose key changed changed, and does not offer Share', async () => {
    changedEndorser.mockReturnValue({ email: 'sam@dimagi.com', status: 'changed', jkt: T2 })
    await unlocked()
    fireEvent.click(screen.getByLabelText('Eva'))
    expect(screen.getByText(/sam@dimagi\.com.*vault key changed/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Share' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('keeps the plaintext out of the page', async () => {
    const { container } = open()
    fireEvent.click(await screen.findByRole('button', { name: /unlock/i }))
    await screen.findByText(/Connected as/)
    expect(container.innerHTML).not.toMatch(/refresh_token|access_token/)
  })
})

const apiError = (status: number, code: string) =>
  new ApiError(status, 'x', { status, code, error: code })

describe('ShareConnectionPage: fixes', () => {
  it.each(['statement_stale', 'zk_value_changed'])(
    'on %s reloads the state, opens again with the key in hand and keeps the ticks', async (code) => {
      await unlocked()
      fireEvent.click(screen.getByLabelText('Eva'))
      const fresh = { ...STATE, sealed_digest: 'new' }
      putShare.mockRejectedValueOnce(apiError(409, code))
      getShareState.mockResolvedValueOnce(fresh)
      fireEvent.click(screen.getByRole('button', { name: 'Share' }))
      expect(await screen.findByText('This connection changed in another tab or computer. Loading it again.')).toBeTruthy()
      await waitFor(() => expect(openForOwner).toHaveBeenCalledTimes(2))
      expect(openForOwner.mock.calls[1]).toEqual([fresh, ME])
      expect(unlock).toHaveBeenCalledTimes(1)
      expect((screen.getByLabelText('Eva') as HTMLInputElement).checked).toBe(true)
      fireEvent.click(screen.getByRole('button', { name: 'Share' }))
      await waitFor(() => expect(putShare).toHaveBeenCalledTimes(2))
      expect(buildShare.mock.calls[1][0]).toBe(fresh)
    })

  it('drops an earlier agent or computer no longer listed, says so, and does not send it', async () => {
    openForOwner.mockResolvedValue({
      account: ACCOUNT,
      statement: { agents: ['12', '99'], my_computers: [T1, T2], endorsers: [], version: 2 },
    })
    await unlocked()
    expect(screen.getByTestId('dropped').textContent).toContain('agent 99')
    expect(screen.getByTestId('dropped').textContent).toContain('BBBB')
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    await waitFor(() => expect(buildShare).toHaveBeenCalled())
    expect(buildShare.mock.calls[0][2]).toEqual({ agents: ['12'], myComputers: [T1], canopyWeb: false })
  })

  it('sends no computers for an agent-owned connection', async () => {
    getShareState.mockResolvedValue({ ...STATE, owner_agent: { id: '12', slug: 'eva', name: 'Eva' } })
    openForOwner.mockResolvedValue({
      account: ACCOUNT, statement: { agents: ['12'], my_computers: [T1], endorsers: [], version: 2 },
    })
    await unlocked()
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    await waitFor(() => expect(buildShare).toHaveBeenCalled())
    expect(buildShare.mock.calls[0][2]).toEqual({ agents: ['12'], myComputers: [], canopyWeb: false })
  })

  it.each(['no-scalar', 'bad-owner-key'])('%s asks to unlock again, not to Reconnect', async (code) => {
    await unlocked()
    fireEvent.click(screen.getByLabelText('Eva'))
    buildShare.mockRejectedValueOnce(new ShareRefused(code, 'd'))
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toBe('Unlock your vault key and try again.')
    expect(screen.queryByRole('link', { name: 'Reconnect' })).toBeNull()
    expect(screen.getByRole('button', { name: /unlock/i })).toBeTruthy()
  })

  it('a 404 on load says it is not theirs, with a link back', async () => {
    getShareState.mockRejectedValue(apiError(404, ''))
    open()
    expect(await screen.findByText("This connection isn't yours to share.")).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Back' }).getAttribute('href')).toBe('/settings/connections')
  })

  it("not_the_owner_key shows the server's reason and the way out, not \"isn't yours\"", async () => {
    const detail = 'Only the vault key this connection was sealed to can share it, and yours is not that key now.'
    getShareState.mockRejectedValue(new ApiError(403, detail, { status: 403, error: 'not_the_owner_key', detail }))
    open()
    expect(await screen.findByText(detail)).toBeTruthy()
    expect(screen.getByText('To share it, disconnect it in Settings, then connect it again.')).toBeTruthy()
    expect(screen.queryByText("This connection isn't yours to share.")).toBeNull()
    expect(screen.getByRole('link', { name: 'Back' }).getAttribute('href')).toBe('/settings/connections')
  })

  it('any other 403 does not claim it is not theirs', async () => {
    getShareState.mockRejectedValue(new ApiError(403, 'x', { status: 403, error: 'step_up_failed', detail: 'Try again.' }))
    open()
    expect(await screen.findByText("You can't open this connection's sharing right now. Try again.")).toBeTruthy()
    expect(screen.queryByText("This connection isn't yours to share.")).toBeNull()
  })

  it('goes back through the router to return_to, not a full page load', async () => {
    getShareState.mockRejectedValue(apiError(403, 'not_the_owner_key'))
    open('?return_to=%2Fagents%2Feva%2Fsettings%2Faccounts')
    fireEvent.click(await screen.findByRole('link', { name: 'Back' }))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/agents/eva/settings/accounts'))
  })

  it('falls back to Settings when return_to is absent or unsafe', async () => {
    getShareState.mockRejectedValue(apiError(403, 'not_the_owner_key'))
    open('?return_to=%2F%2Fevil.test')
    expect((await screen.findByRole('link', { name: 'Back' })).getAttribute('href')).toBe('/settings')
  })
})

describe('safeReturnTo', () => {
  it.each([
    ['/settings/connections', '/settings/connections'],
    ['/w/dimagi/settings?x=1', '/w/dimagi/settings?x=1'],
    ['https://evil.example/x', '/settings'],
    ['//evil.example/x', '/settings'],
    ['/\\evil.example', '/settings'],
    ['javascript:alert(1)', '/settings'],
    ['', '/settings'],
    [null, '/settings'],
  ])('%s -> %s', (given, want) => expect(safeReturnTo(given)).toBe(want))
})

describe('ShareConnectionPage: project screens and apps', () => {
  const SERVER = { available: true, thumbprint: T1, public_jwk: {}, on: false, statements_enabled: true, provider_calls: true, reachable: true }
  const withServer = (over: Record<string, unknown> = {}, rest: Record<string, unknown> = {}) =>
    getShareState.mockResolvedValue({ ...STATE, server: { ...SERVER, ...over }, ...rest })
  const box = () => screen.queryByLabelText('Project screens and apps') as HTMLInputElement | null

  it('shows the box with its one sentence, unticked when the row is off', async () => {
    withServer()
    await unlocked()
    expect(box()?.checked).toBe(false)
    expect(box()?.disabled).toBe(false)
    expect(screen.getByText('canopy-web can use this account for you, with nobody present. If canopy-web were ever hacked, this account could be taken until you disconnect it.')).toBeTruthy()
  })

  it('starts ticked on an unshared row that is on', async () => {
    withServer({ on: true })
    await unlocked()
    expect(box()?.checked).toBe(true)
  })

  it('hides the box when canopy-web has no key', async () => {
    withServer({ available: false })
    await unlocked()
    expect(box()).toBeNull()
  })

  it('hides the box when canopy-web makes no calls for this provider', async () => {
    withServer({ provider_calls: false })
    await unlocked()
    expect(box()).toBeNull()
  })

  it('is disabled and unticked, with its sentence, when statements are off and the row is on', async () => {
    withServer({ statements_enabled: false, on: true })
    await unlocked()
    expect(box()?.checked).toBe(false)
    expect(box()?.disabled).toBe(true)
    expect(screen.getByTestId('canopy-web-unavailable').textContent)
      .toBe('Sharing with agents or your computers turns off project screens and apps for now.')
  })

  it('is disabled with the other sentence when statements are off and the row is off', async () => {
    withServer({ statements_enabled: false })
    await unlocked()
    expect(box()?.disabled).toBe(true)
    expect(screen.getByTestId('canopy-web-unavailable').textContent).toBe("This isn't available yet.")
  })

  it('nothingToShare is false with only canopy-web ticked, and the choice carries it', async () => {
    withServer()
    await unlocked()
    const share = screen.getByRole('button', { name: 'Share' }) as HTMLButtonElement
    expect(share.disabled).toBe(true)
    fireEvent.click(box() as HTMLInputElement)
    expect(share.disabled).toBe(false)
    fireEvent.click(share)
    await waitFor(() => expect(putShare).toHaveBeenCalled())
    expect(buildShare.mock.calls[0][2]).toEqual({ agents: [], myComputers: [], canopyWeb: true })
  })

  it('says a mismatched key in a sentence, without calling the connection unsafe', async () => {
    withServer()
    buildShare.mockRejectedValue(new ShareRefused('server-key', 'The key canopy-web gave does not match its fingerprint: nothing was sent.'))
    await unlocked()
    fireEvent.click(box() as HTMLInputElement)
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    expect((await screen.findByRole('alert')).textContent).toContain('does not match its fingerprint')
    expect(screen.queryByRole('link', { name: 'Reconnect' })).toBeNull()
  })

  it('seeds an active row from the verified statement, not from a forged server.on', async () => {
    openForOwner.mockResolvedValue({ account: ACCOUNT, statement: { agents: ['12'], my_computers: [] } })
    withServer({ on: true }, { state: 'active' })
    await unlocked()
    expect(box()?.checked).toBe(false)
  })

  it('ticks an active row whose verified statement names canopy-web, even if server.on is false', async () => {
    openForOwner.mockResolvedValue({ account: ACCOUNT, statement: { agents: ['12'], my_computers: [], canopy_web: T1 } })
    withServer({ on: false }, { state: 'active' })
    await unlocked()
    expect(box()?.checked).toBe(true)
    expect(screen.queryByTestId('canopy-web-key-changed')).toBeNull()
  })

  it('warns once, and keeps the box, when the statement names another key than the one canopy-web has now', async () => {
    openForOwner.mockResolvedValue({ account: ACCOUNT, statement: { agents: ['12'], my_computers: [], canopy_web: T2 } })
    withServer({}, { state: 'active' })
    await unlocked()
    expect(box()?.disabled).toBe(false)
    expect(screen.getByTestId('canopy-web-key-changed').textContent)
      .toBe("canopy-web's key has changed. Turn this off, or check with your administrator before keeping it on.")
    fireEvent.click(box() as HTMLInputElement)
    expect(screen.queryByTestId('canopy-web-key-changed')).toBeNull()
  })

  it('does not call the key changed when canopy-web has no key to compare (unreachable)', async () => {
    openForOwner.mockResolvedValue({ account: ACCOUNT, statement: { agents: ['12'], my_computers: [], canopy_web: T2 } })
    withServer({ thumbprint: '' }, { state: 'active' })
    await unlocked()
    expect(screen.queryByTestId('canopy-web-key-changed')).toBeNull()
  })

  it('still warns about a reconnect after canopy-web wrote while unshared', async () => {
    openForOwner.mockResolvedValue({ account: ACCOUNT, statement: { agents: ['40'], my_computers: [], version: 2 } })
    withServer({}, { sealed_sig: { by: { kind: 'device', target: T1 } } })
    await unlocked()
    expect(screen.getByText(/was reconnected/)).toBeTruthy()
  })

  it('says sharing now turns it off when canopy-web cannot be reached and the row is on', async () => {
    withServer({ available: false, on: true })
    await unlocked()
    expect(box()).toBeNull()
    expect(screen.getByTestId('canopy-web-unreachable').textContent)
      .toBe("Sharing now turns off project screens and apps, because canopy-web can't be reached right now.")
  })

  it('says nothing about it when canopy-web cannot be reached and the row is off', async () => {
    withServer({ available: false, on: false })
    await unlocked()
    expect(screen.queryByTestId('canopy-web-unreachable')).toBeNull()
  })
})
