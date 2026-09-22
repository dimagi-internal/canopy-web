// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const getAgentInterface = vi.fn()
const saveAgentInterface = vi.fn()
const unpublishAgentInterface = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getAgentInterface: (...a: unknown[]) => getAgentInterface(...a),
  saveAgentInterface: (...a: unknown[]) => saveAgentInterface(...a),
  unpublishAgentInterface: (...a: unknown[]) => unpublishAgentInterface(...a),
}))

const { AgentInterfaceView } = await import('./AgentInterfaceView')

const PUBLISHED = {
  interface: {
    full: ['contact@dimagi.com:verified'],
    capabilities: { ask: {
      description: 'Ask ACE.', callers: ['contact'],
      tools: ['Read'], bash: ['canopy email read --repo . {thread_id}'], input: { opportunity_id: 'integer' },
    } },
  },
  source: 'full: [contact@dimagi.com:verified]\n# comment kept\n',
  published_at: '2026-09-21T12:00:00Z', published_by_email: 'op@dimagi.com',
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentInterfaceView', () => {
  it('says plainly when nothing is published, and offers no edit to a non-admin', async () => {
    getAgentInterface.mockResolvedValue({ interface: {}, source: '', published_at: null, published_by_email: null })
    render(<AgentInterfaceView agentSlug="ace" />)
    expect((await screen.findByTestId('interface-none')).textContent).toMatch(/gets all of it/)
    expect(screen.queryByRole('button', { name: /Set up|Edit/ })).toBeNull()
  })

  it('shows domain-wide access, each capability, its MCP tool and inputs', async () => {
    getAgentInterface.mockResolvedValue(PUBLISHED)
    render(<AgentInterfaceView agentSlug="ace" />)
    expect((await screen.findByTestId('interface-full')).textContent).toContain('contact@dimagi.com:verified')
    const row = screen.getByTestId('interface-capability')
    expect(row.textContent).toContain('MCP: ace__ask (opportunity_id: integer)')
    expect(row.textContent).toContain('canopy email read --repo . {thread_id}')
  })

  it('an admin edits the YAML as saved — comments included — and sees it saved', async () => {
    getAgentInterface.mockResolvedValue(PUBLISHED)
    saveAgentInterface.mockResolvedValue({ ...PUBLISHED, source: 'full: [contact@dimagi-ai.com:verified]\n' })
    render(<AgentInterfaceView agentSlug="ace" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    const box = screen.getByLabelText('Interface (YAML)') as HTMLTextAreaElement
    expect(box.value).toContain('# comment kept')
    fireEvent.change(box, { target: { value: 'full: [contact@dimagi-ai.com:verified]\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveAgentInterface).toHaveBeenCalledWith('ace', 'full: [contact@dimagi-ai.com:verified]\n'))
    expect(await screen.findByTestId('interface-full')).toBeTruthy()
  })

  it("shows the server's reason when the YAML is refused, and keeps the draft", async () => {
    getAgentInterface.mockResolvedValue(PUBLISHED)
    saveAgentInterface.mockRejectedValue(new Error("full: 'everyone' is not a caller class"))
    render(<AgentInterfaceView agentSlug="ace" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Interface (YAML)'), { target: { value: 'full: [everyone]' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/not a caller class/)
    expect((screen.getByLabelText('Interface (YAML)') as HTMLTextAreaElement).value).toBe('full: [everyone]')
  })
})
