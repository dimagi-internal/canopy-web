// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AccessConversionOut } from '@/api/agents'

const previewAccessConversion = vi.fn()
const applyAccessConversion = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  previewAccessConversion: (...a: unknown[]) => previewAccessConversion(...a),
  applyAccessConversion: (...a: unknown[]) => applyAccessConversion(...a),
}))

const { AccessMove } = await import('./AccessMove')

const PLAN = (o: Partial<AccessConversionOut> = {}): AccessConversionOut => ({
  already: false, ok: true, probed: 42, unsupported: [], differences: [],
  lines: [
    { who: 'A known visitor or sender at @dimagi.com (verified)', role: 'viewer', manual_only: false },
    { who: 'Everyone in connect, editor or above', role: 'editor', manual_only: true },
  ],
  ...o,
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const show = (canEdit = true, onMoved = vi.fn()) =>
  render(<AccessMove agentSlug="hal" agentName="Hal" canEdit={canEdit} onMoved={onMoved} />)

describe('AccessMove', () => {
  it('previews the lines and says nobody changes, then moves the agent and reloads', async () => {
    previewAccessConversion.mockResolvedValue(PLAN())
    applyAccessConversion.mockResolvedValue({})
    const onMoved = vi.fn()
    show(true, onMoved)
    fireEvent.click(screen.getByRole('button', { name: 'See what moving would do' }))
    const planBox = await screen.findByTestId('access-move-plan')
    expect(planBox.textContent).toContain('changes nobody’s access (42 checks')
    expect(planBox.textContent).toContain('A known visitor or sender at @dimagi.com (verified): viewer')
    expect(planBox.textContent).toContain('Everyone in connect, editor or above: editor, needs approval before anything goes out')
    fireEvent.click(screen.getByRole('button', { name: 'Move Hal onto the access list' }))
    await waitFor(() => expect(applyAccessConversion).toHaveBeenCalledWith('hal'))
    await waitFor(() => expect(onMoved).toHaveBeenCalled())
  })

  it('offers no move when it would change someone’s access, and says who', async () => {
    previewAccessConversion.mockResolvedValue(
      PLAN({ ok: false, lines: [], differences: ['member vw@dimagi.com (viewer) [canopy]: (none) -> (confined)'] }),
    )
    show()
    fireEvent.click(screen.getByRole('button', { name: 'See what moving would do' }))
    expect((await screen.findByTestId('access-move-plan')).textContent).toContain('vw@dimagi.com')
    expect(screen.queryByRole('button', { name: /Move Hal onto/ })).toBeNull()
  })

  it('shows the server’s refusal inline', async () => {
    previewAccessConversion.mockResolvedValue(PLAN())
    applyAccessConversion.mockRejectedValue(new Error('moving this agent would change someone’s access'))
    show()
    fireEvent.click(screen.getByRole('button', { name: 'See what moving would do' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Move Hal onto the access list' }))
    expect((await screen.findByRole('alert')).textContent).toContain('would change someone’s access')
  })

  it('is only a note for someone who cannot change the list', () => {
    show(false)
    expect(screen.getByTestId('access-move').textContent).toContain('has not moved onto this list yet')
    expect(screen.queryByRole('button')).toBeNull()
  })
})
