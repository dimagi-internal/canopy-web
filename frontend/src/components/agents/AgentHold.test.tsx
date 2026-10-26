// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { AgentHold } from '@/api/agents'

const holdAgent = vi.fn<(slug: string, note: string) => Promise<AgentHold>>()
const releaseAgentHold = vi.fn<(slug: string) => Promise<AgentHold>>()

vi.mock('@/api/agents', () => ({ holdAgent, releaseAgentHold }))

const { AgentHoldBanner, AgentHoldControl } = await import('./AgentHold')

const OFF: AgentHold = { held: false, note: '', held_at: null, held_by_email: '', queued: 0 }
const ON: AgentHold = {
  held: true, note: 'debugging', held_at: '2026-10-26T11:42:12Z', held_by_email: 'jj@dimagi.com', queued: 3,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentHoldBanner', () => {
  it('shows nothing while the agent runs', () => {
    render(<AgentHoldBanner slug="ace" name="ACE" hold={OFF} canEdit onChange={() => {}} />)
    expect(screen.queryByTestId('agent-hold-banner')).toBeNull()
  })

  it('says why and what waits, and releases', async () => {
    releaseAgentHold.mockResolvedValue(OFF)
    const onChange = vi.fn()
    render(<AgentHoldBanner slug="ace" name="ACE" hold={ON} canEdit onChange={onChange} />)
    expect(screen.getByTestId('agent-hold-banner').textContent).toContain('debugging')
    expect(screen.getByTestId('agent-hold-banner').textContent).toContain('3 queued')

    fireEvent.click(screen.getByTestId('agent-hold-release'))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(OFF))
    expect(releaseAgentHold).toHaveBeenCalledWith('ace')
  })

  it('offers no release to someone who cannot', () => {
    render(<AgentHoldBanner slug="ace" name="ACE" hold={ON} canEdit={false} onChange={() => {}} />)
    expect(screen.queryByTestId('agent-hold-release')).toBeNull()
  })
})

describe('AgentHoldControl', () => {
  it('puts the agent on hold with the note', async () => {
    holdAgent.mockResolvedValue(ON)
    const onChange = vi.fn()
    render(<AgentHoldControl slug="ace" hold={OFF} canEdit onChange={onChange} />)

    fireEvent.change(screen.getByLabelText('Hold note'), { target: { value: ' debugging ' } })
    fireEvent.click(screen.getByTestId('agent-hold-control-hold'))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(ON))
    expect(holdAgent).toHaveBeenCalledWith('ace', 'debugging')
  })

  it('says when the change failed', async () => {
    holdAgent.mockRejectedValue(new Error('this action requires the editor role or above'))
    render(<AgentHoldControl slug="ace" hold={OFF} canEdit onChange={() => {}} />)

    fireEvent.click(screen.getByTestId('agent-hold-control-hold'))

    await screen.findByText('this action requires the editor role or above')
  })
})
