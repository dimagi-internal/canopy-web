// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

const api = vi.hoisted(() => ({ setAgentGoogleAccount: vi.fn() }))
vi.mock('@/api/agents', () => api)

const { AgentGoogleAccountControl } = await import('./AgentGoogleAccountControl')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const agent = { slug: 'scout', name: 'Scout', email: 'scout@dimagi.ai', google_account: '' } as never

describe('AgentGoogleAccountControl', () => {
  it('sets the Google account apart from a canopy-hosted mailbox', async () => {
    api.setAgentGoogleAccount.mockResolvedValue('scout@dimagi-ai.com')
    render(<AgentGoogleAccountControl agent={agent} canEdit />)
    expect(screen.getByText(/same as its mailbox/)).toBeTruthy()
    fireEvent.click(screen.getByTestId('agent-google-account-edit'))
    fireEvent.change(screen.getByTestId('agent-google-account-input'), { target: { value: ' scout@dimagi-ai.com ' } })
    fireEvent.click(screen.getByTestId('agent-google-account-save'))
    expect(await screen.findByText('scout@dimagi-ai.com')).toBeTruthy()
    expect(api.setAgentGoogleAccount).toHaveBeenCalledWith(agent, 'scout@dimagi-ai.com')
  })

  it('is read-only to someone who may not change it', () => {
    render(<AgentGoogleAccountControl agent={agent} canEdit={false} />)
    expect(screen.queryByTestId('agent-google-account-edit')).toBeNull()
  })
})
