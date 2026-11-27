// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const order: string[] = []
const getSealing = vi.fn()
const putSealedSession = vi.fn()
const approveWithPasskey = vi.fn()
const mint = vi.fn()
vi.mock('@/api/awsSealed', () => ({
  getSealing: (...a: unknown[]) => getSealing(...a),
  putSealedSession: (...a: unknown[]) => putSealedSession(...a),
}))
vi.mock('@/api/approvals', () => ({ approveWithPasskey: (...a: unknown[]) => approveWithPasskey(...a) }))
vi.mock('@/aws/sealedSession', () => ({
  mintSealedSession: (...a: unknown[]) => mint(...a),
  SealedSessionError: class extends Error {},
}))
vi.mock('@/vault/useVaultUnlock', () => ({
  useVaultUnlock: () => ({ unlock: async () => { order.push('unlock'); return { userId: '7' } }, element: null }),
}))
const pinned: unknown[] = []
vi.mock('@/vault/trust', () => ({ savePins: (p: unknown) => pinned.push(p) }))

import type { ApprovalCard } from '@/api/approvals'
import { AwsSealedApprove } from './AwsSealedApprove'

const CARD = { id: 9, can_decide: true, status: 'pending' } as unknown as ApprovalCard
const STEP = { agent: 'ace', request: 5 }

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  order.length = 0
})

describe('AwsSealedApprove', () => {
  it('makes and seals the keys first, then approves with the passkey, then hands them over', async () => {
    getSealing.mockImplementation(async () => { order.push('sealing'); return { grant: 5 } })
    mint.mockImplementation(async () => { order.push('mint'); return { kind: 'sealed', valueZk: 'zks1:x', expiresAt: '2030-01-01T01:00:00Z', machines: 1 } })
    approveWithPasskey.mockImplementation(async () => { order.push('approve'); return { ...CARD, status: 'done' } })
    putSealedSession.mockImplementation(async () => { order.push('hand-over') })
    const onCard = vi.fn()
    render(<AwsSealedApprove card={CARD} step={STEP} comment="ok" onCard={onCard} />)
    fireEvent.click(screen.getByTestId('aws-sealed-run'))
    await waitFor(() => expect(screen.getByTestId('aws-sealed-done')).toBeTruthy())
    expect(order).toEqual(['unlock', 'sealing', 'mint', 'approve', 'hand-over'])
    expect(approveWithPasskey).toHaveBeenCalledWith(9, 'ok')
    expect(putSealedSession).toHaveBeenCalledWith('ace', 5, { value_zk: 'zks1:x', expires_at: '2030-01-01T01:00:00Z' })
    expect(onCard).toHaveBeenCalled()
  })

  it('asks about people not trusted yet before making anything, and pins them when accepted', async () => {
    getSealing.mockResolvedValue({ grant: 5 })
    mint.mockResolvedValueOnce({ kind: 'trust', people: [{ by: '9', email: 'bo@dimagi.com', jkt: 'abcd', status: 'new', covers: [] }] })
      .mockResolvedValueOnce({ kind: 'sealed', valueZk: 'zks1:y', expiresAt: '2030-01-01T01:00:00Z', machines: 1 })
    approveWithPasskey.mockResolvedValue({ ...CARD, status: 'done' })
    putSealedSession.mockResolvedValue({})
    render(<AwsSealedApprove card={CARD} step={STEP} comment="" onCard={() => undefined} />)
    fireEvent.click(screen.getByTestId('aws-sealed-run'))
    expect((await screen.findByTestId('aws-sealed-trust')).textContent).toContain('bo@dimagi.com')
    expect(approveWithPasskey).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('aws-sealed-trust-accept'))
    await waitFor(() => expect(putSealedSession).toHaveBeenCalled())
    expect(pinned).toHaveLength(1)
    expect([...mint.mock.calls[1][2]]).toEqual(['9'])
  })

  it('renewing makes new keys without approving again, and a failed hand-over can be retried', async () => {
    getSealing.mockResolvedValue({ grant: 5 })
    mint.mockResolvedValue({ kind: 'sealed', valueZk: 'zks1:z', expiresAt: '2030-01-01T01:00:00Z', machines: 1 })
    putSealedSession.mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({})
    render(<AwsSealedApprove card={{ ...CARD, status: 'done', can_decide: false }} step={STEP}
                             comment="" renew onCard={() => undefined} />)
    fireEvent.click(screen.getByTestId('aws-sealed-run'))
    expect((await screen.findByTestId('aws-sealed-error')).textContent).toContain('network')
    expect(approveWithPasskey).not.toHaveBeenCalled()
    expect(screen.getByTestId('aws-sealed-run').textContent).toContain('Hand the keys over again')
    fireEvent.click(screen.getByTestId('aws-sealed-run'))
    await waitFor(() => expect(screen.getByTestId('aws-sealed-done')).toBeTruthy())
    expect(mint).toHaveBeenCalledTimes(1)   // the same sealed keys, not new ones
  })

  it('a cancelled passkey approval keeps the sealed keys and asks to approve again, never to hand over unapproved', async () => {
    getSealing.mockResolvedValue({ grant: 5 })
    mint.mockResolvedValue({ kind: 'sealed', valueZk: 'zks1:w', expiresAt: '2030-01-01T01:00:00Z', machines: 1 })
    approveWithPasskey.mockRejectedValueOnce(new Error('The passkey was cancelled')).mockResolvedValueOnce({ ...CARD, status: 'done' })
    putSealedSession.mockResolvedValue({})
    render(<AwsSealedApprove card={CARD} step={STEP} comment="" onCard={() => undefined} />)
    fireEvent.click(screen.getByTestId('aws-sealed-run'))
    expect((await screen.findByTestId('aws-sealed-error')).textContent).toContain('cancelled')
    expect(putSealedSession).not.toHaveBeenCalled()
    expect(screen.getByTestId('aws-sealed-run').textContent).toBe('Approve with your passkey')
    fireEvent.click(screen.getByTestId('aws-sealed-run'))
    await waitFor(() => expect(screen.getByTestId('aws-sealed-done')).toBeTruthy())
    expect(approveWithPasskey).toHaveBeenCalledTimes(2)
    expect(mint).toHaveBeenCalledTimes(1)
  })
})
