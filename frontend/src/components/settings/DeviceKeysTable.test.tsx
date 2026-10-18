// @vitest-environment jsdom
//
// Runner device keys (Canopy Vault, canopy-web#1538): a pending key is approved
// from here, an active one revoked, and only by someone who manages its runner.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { approveRunnerDeviceKey, revokeRunnerDeviceKey } = vi.hoisted(() => ({
  approveRunnerDeviceKey: vi.fn(),
  revokeRunnerDeviceKey: vi.fn(),
}))
vi.mock('@/api/deviceKeys', () => ({ approveRunnerDeviceKey, revokeRunnerDeviceKey }))
const { giveAccess, approveAndGrant } = vi.hoisted(() => ({ giveAccess: vi.fn(), approveAndGrant: vi.fn() }))
vi.mock('@/vault/operations', () => ({ giveAccess, approveAndGrant, unlockVaultKey: vi.fn() }))

const { DeviceKeysTable } = await import('./DeviceKeysTable')

const KEY = {
  id: 'k1', runner: 'r1', runner_name: 'jj-mbp', jkt: 'abcdefghijklmnopqrstuvwxyz', kind: 'secure-enclave' as const,
  label: 'laptop', role_arn: '', status: 'pending' as const, created_at: '2026-10-16T00:00:00Z', approved_at: null,
  approved_by_email: '', revoked_at: null, last_used_at: null, can_manage: true,
  ecdh_jkt: '', ecdh_public_jwk: {}, zk_pending_agents: [] as string[], reapproval: false, ecdh_waiting: false,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('DeviceKeysTable', () => {
  it('shows an online machine whose new key waits, and approves it in one touch', async () => {
    const waiting = { ...KEY, status: 'active' as const, ecdh_waiting: true, zk_pending_agents: ['hal', 'eva'] }
    approveAndGrant.mockResolvedValue({ key: { ...waiting, ecdh_waiting: false }, granted: ['hal', 'eva'], failed: [] })
    render(<DeviceKeysTable keys={[waiting]} onChange={vi.fn()} />)
    expect(screen.getByTestId('device-key-ecdh-waiting-k1').textContent).toMatch(/New key waiting/)
    expect(screen.getByText('active')).toBeTruthy()
    expect(screen.queryByTestId('device-key-give-k1')).toBeNull()
    fireEvent.click(screen.getByTestId('device-key-approve-k1'))
    await waitFor(() => expect(approveAndGrant).toHaveBeenCalledWith('k1', ['hal', 'eva'], expect.any(Function)))
  })

  it('approves and grants in one go when zero-knowledge agents wait on the key (canopy-web#1550)', async () => {
    approveAndGrant.mockResolvedValue({ key: { ...KEY, status: 'active' }, granted: ['eva'], failed: [] })
    const onChange = vi.fn()
    render(<DeviceKeysTable keys={[{ ...KEY, zk_pending_agents: ['eva'] }]} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ ...KEY, status: 'active' }))
    expect(approveAndGrant).toHaveBeenCalledWith('k1', ['eva'], expect.any(Function))
    expect(approveRunnerDeviceKey).not.toHaveBeenCalled()
    expect(await screen.findByText(/this machine can now read eva/)).toBeTruthy()
    expect(screen.queryByTestId('device-key-zk-grant-button')).toBeNull()
  })

  it('leaves a grant that failed for the Grant button', async () => {
    approveAndGrant.mockResolvedValue({ key: { ...KEY, status: 'active' }, granted: [], failed: ['eva'] })
    render(<DeviceKeysTable keys={[{ ...KEY, zk_pending_agents: ['eva'] }]} onChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByTestId('device-key-zk-grant-button')).toBeTruthy()
  })

  it('approves a pending key and reports the new state', async () => {
    approveRunnerDeviceKey.mockResolvedValue({ ...KEY, status: 'active' })
    const onChange = vi.fn()
    render(<DeviceKeysTable keys={[KEY]} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ ...KEY, status: 'active' }))
    expect(approveRunnerDeviceKey).toHaveBeenCalledWith('k1')
  })

  it('offers access right after approving, for agents the listing did not know about (canopy-web#1550)', async () => {
    approveRunnerDeviceKey.mockResolvedValue({ ...KEY, status: 'active', zk_pending_agents: ['eva'] })
    giveAccess.mockResolvedValue({})
    render(<DeviceKeysTable keys={[KEY]} onChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect((await screen.findByTestId('device-key-zk-grant')).textContent).toContain('eva')
    fireEvent.click(screen.getByTestId('device-key-zk-grant-button'))
    await waitFor(() => expect(giveAccess).toHaveBeenCalledWith('eva', expect.any(Function)))
    expect((await screen.findByText(/this machine can now read eva/))).toBeTruthy()
  })

  it('an approved machine agents still wait on: one click gives it access', async () => {
    giveAccess.mockResolvedValue({})
    const onChange = vi.fn()
    const k = { ...KEY, status: 'active' as const, zk_pending_agents: ['hal', 'eva'] }
    render(<DeviceKeysTable keys={[k]} onChange={onChange} />)
    fireEvent.click(screen.getByTestId('device-key-give-k1'))
    await waitFor(() => expect(giveAccess).toHaveBeenCalledTimes(2))
    expect(giveAccess.mock.calls.map((c) => c[0])).toEqual(['hal', 'eva'])
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ ...k, zk_pending_agents: [] }))
  })

  it('says a re-approval is an encryption key added, not a new box', () => {
    render(<DeviceKeysTable keys={[{ ...KEY, reapproval: true, ecdh_jkt: 'j' }]} onChange={vi.fn()} />)
    expect(screen.getByTestId('device-key-reapproval-k1').textContent).toBe('re-approval: encryption key added')
  })

  it('revokes after confirming', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    revokeRunnerDeviceKey.mockResolvedValue({ ...KEY, status: 'revoked' })
    const onChange = vi.fn()
    render(<DeviceKeysTable keys={[{ ...KEY, status: 'active' }]} onChange={onChange} />)
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(revokeRunnerDeviceKey).toHaveBeenCalledWith('k1'))
  })

  it('keeps an EC2 box’s instance role on hover, not on the page (canopy-web#1549)', () => {
    const arn = 'arn:aws:iam::123456789012:role/canopy-runner'
    render(<DeviceKeysTable keys={[{ ...KEY, kind: 'aws-iam', label: '', role_arn: arn }]} onChange={vi.fn()} />)
    expect(screen.getByText('aws-iam').getAttribute('title')).toBe(arn)
    expect(screen.queryByText(/123456789012/)).toBeNull()
  })

  it('offers no actions on a key whose runner you do not manage', () => {
    render(<DeviceKeysTable keys={[{ ...KEY, can_manage: false }]} onChange={vi.fn()} />)
    expect(screen.queryByRole('button')).toBeNull()
  })
})
