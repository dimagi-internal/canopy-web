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
const { grantPending } = vi.hoisted(() => ({ grantPending: vi.fn() }))
vi.mock('@/vault/operations', () => ({ grantPending, unlockVaultKey: vi.fn() }))

const { DeviceKeysTable } = await import('./DeviceKeysTable')

const KEY = {
  id: 'k1', runner: 'r1', runner_name: 'jj-mbp', jkt: 'abcdefghijklmnopqrstuvwxyz', kind: 'secure-enclave' as const,
  label: 'laptop', role_arn: '', status: 'pending' as const, created_at: '2026-10-16T00:00:00Z', approved_at: null,
  approved_by_email: '', revoked_at: null, last_used_at: null, can_manage: true,
  ecdh_jkt: '', ecdh_public_jwk: {}, zk_pending_agents: [] as string[], reapproval: false,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('DeviceKeysTable', () => {
  it('approves a pending key and reports the new state', async () => {
    approveRunnerDeviceKey.mockResolvedValue({ ...KEY, status: 'active' })
    const onChange = vi.fn()
    render(<DeviceKeysTable keys={[KEY]} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ ...KEY, status: 'active' }))
    expect(approveRunnerDeviceKey).toHaveBeenCalledWith('k1')
  })

  it('offers the vault-key grant right after approving, for zero-knowledge agents (canopy-web#1550)', async () => {
    approveRunnerDeviceKey.mockResolvedValue({ ...KEY, status: 'active', zk_pending_agents: ['eva'] })
    grantPending.mockResolvedValue({})
    render(<DeviceKeysTable keys={[KEY]} onChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect((await screen.findByTestId('device-key-zk-grant')).textContent).toContain('eva')
    fireEvent.click(screen.getByTestId('device-key-zk-grant-button'))
    await waitFor(() => expect(grantPending).toHaveBeenCalledWith('eva', expect.any(Function)))
    expect((await screen.findByText(/Granted: eva/))).toBeTruthy()
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

  it('names an EC2 box by its instance role (canopy-web#1549)', () => {
    render(
      <DeviceKeysTable
        keys={[{ ...KEY, kind: 'aws-iam', label: '', role_arn: 'arn:aws:iam::123456789012:role/canopy-runner' }]}
        onChange={vi.fn()}
      />,
    )
    expect(screen.getByText('123456789012:role/canopy-runner')).toBeTruthy()
    expect(screen.getByText('aws-iam')).toBeTruthy()
  })

  it('offers no actions on a key whose runner you do not manage', () => {
    render(<DeviceKeysTable keys={[{ ...KEY, can_manage: false }]} onChange={vi.fn()} />)
    expect(screen.queryByRole('button')).toBeNull()
  })
})
