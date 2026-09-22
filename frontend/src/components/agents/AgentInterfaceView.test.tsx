// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const getAgentInterface = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getAgentInterface: (...a: unknown[]) => getAgentInterface(...a),
}))

const { AgentInterfaceView } = await import('./AgentInterfaceView')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentInterfaceView', () => {
  it('says plainly when nothing is published', async () => {
    getAgentInterface.mockResolvedValue({ interface: {}, published_at: null, published_by_email: null })
    render(<AgentInterfaceView agentSlug="ace" />)
    expect((await screen.findByTestId('interface-none')).textContent).toMatch(/gets all of it/)
  })

  it('lists each capability, who may use it and what it allows', async () => {
    getAgentInterface.mockResolvedValue({
      interface: { capabilities: { ask: {
        description: 'Ask ACE.', callers: ['contact:verified', 'member'],
        tools: ['Read'], bash: ['canopy email read --repo . {thread_id}'], input: { opportunity_id: 'integer' },
      } } },
      published_at: '2026-09-21T12:00:00Z', published_by_email: 'op@dimagi.com',
    })
    render(<AgentInterfaceView agentSlug="ace" />)
    const row = await screen.findByTestId('interface-capability')
    expect(row.textContent).toContain('ask')
    expect(row.textContent).toContain('contact:verified, member')
    expect(row.textContent).toContain('canopy email read --repo . {thread_id}')
    expect(row.textContent).toContain('MCP: ace__ask (opportunity_id: integer)')
    expect(screen.getByText(/Everything not listed is refused/)).toBeTruthy()
  })
})
