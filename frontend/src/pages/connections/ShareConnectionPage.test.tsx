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

import { ShareConnectionPage, safeReturnTo } from './ShareConnectionPage'
import { ShareRefused } from '@/vault/connectionShare'

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
    expect(screen.getByText(/Connected as 00Dxx\/005yy/)).toBeTruthy()
    expect(screen.queryByText(/Server Label/)).toBeNull()
    expect(openForOwner).toHaveBeenCalledWith(STATE, ME)
    expect(unlock).toHaveBeenCalledTimes(1)
    // machines by name and fingerprint; agents by name
    expect(screen.getByText('jj-laptop')).toBeTruthy()
    expect(screen.getByText('AAAA AAAA AAAA AAAA AAAA AAAA AAAA AAAA')).toBeTruthy()
    expect(screen.getByLabelText('Eva')).toBeTruthy()
  })

  it('sends a signed statement and a re-sealed envelope with the chosen agents', async () => {
    await unlocked()
    fireEvent.click(screen.getByLabelText('Eva'))
    fireEvent.click(screen.getByLabelText(/jj-laptop/))
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    await waitFor(() => expect(putShare).toHaveBeenCalledWith('c1', { sig: 's' }))
    expect(buildShare).toHaveBeenCalledWith(STATE, ME, { agents: ['12'], myComputers: [T1] }, TRUST)
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
    expect(buildShare.mock.calls[0][2]).toEqual({ agents: ['12'], myComputers: [] })
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
    await waitFor(() => expect(buildShare.mock.calls[0][2]).toEqual({ agents: ['40'], myComputers: [T1] }))
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
