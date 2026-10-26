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
  runner_host: '', runner_kind: 'emdash', ecdh_store: '', can_open: [] as { slug: string; name: string }[],
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
    expect(screen.getByTestId('device-key-status-k1').textContent).not.toMatch(/Waiting for your approval/)
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
    expect(await screen.findByText(/this runner can now open eva/)).toBeTruthy()
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
    expect((await screen.findByText(/this runner can now open eva/))).toBeTruthy()
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

  it('says plainly that a new machine waits for your approval, and what approving does', () => {
    render(<DeviceKeysTable keys={[KEY]} onChange={vi.fn()} />)
    expect(screen.getByTestId('device-key-status-k1').textContent).toMatch(/^Waiting for your approval — approving gives it a copy/)
  })

  it('one line per machine: which one, where its key lives, what it can open', () => {
    const mac = { ...KEY, status: 'active' as const, runner_name: 'acedimagi-mbp-cdp',
      runner_host: 'acedimagi@Jonathans-MacBook-Pro.local', approved_by_email: 'jj@dimagi.com',
      can_open: [{ slug: 'hal', name: 'Hal' }, { slug: 'eva', name: 'Eva' }] }
    const cloud = { ...KEY, id: 'k2', status: 'active' as const, kind: 'aws-iam' as const, runner_name: 'cloud-ec2-2',
      runner_host: 'cloud-ec2-2', runner_kind: 'cloud', ecdh_store: 'aws-kms', can_open: [{ slug: 'hal', name: 'Hal' }] }
    render(<DeviceKeysTable keys={[mac, cloud]} onChange={vi.fn()} />)
    expect(screen.getByTestId('device-key-line-k1').textContent).toBe(
      'acedimagi-mbp-cdp — MacBook Pro, acedimagi account · key locked in the security chip · can open Hal, Eva')
    expect(screen.getByTestId('device-key-line-k2').textContent).toBe(
      'cloud-ec2-2 — AWS server · key held by AWS · can open Hal')
    expect(screen.getByTestId('device-key-status-k1').textContent).toBe('Approved by jj@dimagi.com')
    // No table, no key kinds or thumbprints on the page.
    expect(screen.queryByRole('table')).toBeNull()
    expect(document.body.textContent).not.toMatch(/secure-enclave|aws-iam|abcdefghijkl/)
  })

  it('keeps revoked keys out of the way until asked for', () => {
    const old = { ...KEY, id: 'k0', status: 'revoked' as const }
    render(<DeviceKeysTable keys={[{ ...KEY, status: 'active' }, old]} onChange={vi.fn()} />)
    expect(screen.queryByTestId('device-key-k0')).toBeNull()
    fireEvent.click(screen.getByTestId('device-keys-show-revoked'))
    expect(screen.getByTestId('device-key-status-k0').textContent).toMatch(/^Revoked/)
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
    expect(screen.getByTestId('device-key-line-k1').getAttribute('title')).toBe(arn)
    expect(screen.getByTestId('device-key-line-k1').textContent).toMatch(/AWS server · key kept on the server/)
    expect(screen.queryByText(/123456789012/)).toBeNull()
  })

  it('offers no actions on a key whose runner you do not manage', () => {
    render(<DeviceKeysTable keys={[{ ...KEY, can_manage: false }]} onChange={vi.fn()} />)
    expect(screen.queryByRole('button')).toBeNull()
  })
})
