// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { UnclaimableTurn } from '@/api/harness'

// Every stuck turn the warning shows can be closed from the warning itself.

const cancelTurn = vi.fn()
vi.mock('@/api/harness', () => ({
  cancelTurn: (...a: unknown[]) => cancelTurn(...a),
}))

const { StuckTurnsAlert } = await import('./StuckTurnsAlert')

const turn = (id: string, extra: Partial<UnclaimableTurn> = {}): UnclaimableTurn => ({
  turn_id: id, target: 'agent hal', prompt: 'READINESS CHECK', created_at: '2026-10-15T17:34:28Z',
  reason: 'pinned to cloud-ec2-1, which has been retired — it will never run',
  kind: 'config', can_cancel: true, ...extra,
})
const onCancelled = vi.fn()

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('StuckTurnsAlert', () => {
  it('renders nothing with nothing stuck', () => {
    const { container } = render(<StuckTurnsAlert turns={[]} onCancelled={onCancelled} />)
    expect(container.textContent).toBe('')
  })

  it('cancels one turn', async () => {
    cancelTurn.mockResolvedValue({})
    render(<StuckTurnsAlert turns={[turn('a')]} onCancelled={onCancelled} />)
    expect(screen.queryByTestId('stuck-cancel-all')).toBeNull()
    fireEvent.click(screen.getByTestId('stuck-cancel-a'))
    await waitFor(() => expect(onCancelled).toHaveBeenCalledWith(['a']))
  })

  it('cancels all it may, and offers nothing on a turn it may not', async () => {
    cancelTurn.mockResolvedValue({})
    render(<StuckTurnsAlert turns={[turn('a'), turn('b'), turn('c', { can_cancel: false })]}
      onCancelled={onCancelled} />)
    expect(screen.queryByTestId('stuck-cancel-c')).toBeNull()
    fireEvent.click(screen.getByTestId('stuck-cancel-all'))
    await waitFor(() => expect(onCancelled).toHaveBeenCalledWith(['a', 'b']))
    expect(cancelTurn).toHaveBeenCalledTimes(2)
  })

  it('shows a refusal', async () => {
    cancelTurn.mockRejectedValue(new Error('only a queued turn can be cancelled'))
    render(<StuckTurnsAlert turns={[turn('a')]} onCancelled={onCancelled} />)
    fireEvent.click(screen.getByTestId('stuck-cancel-a'))
    expect(await screen.findByTestId('stuck-cancel-error')).toBeTruthy()
    expect(onCancelled).not.toHaveBeenCalled()
  })
})
