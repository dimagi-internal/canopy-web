// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setStepUpHandler } from '@/auth/stepUp'

// The API client builds a `Request` from a path; node's needs an absolute URL,
// a browser's does not. Resolve it against the page, as a browser would.
vi.hoisted(() => {
  const Native = globalThis.Request
  globalThis.Request = class extends Native {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === 'string' ? new URL(input, window.location.href) : input, init)
    }
  } as typeof Request
})

const getAgentInterface = vi.fn()
const getAgentInterfaceHistory = vi.fn()
const saveAgentInterface = vi.fn()
const unpublishAgentInterface = vi.fn()
const setCapabilityPhi = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getAgentInterface: (...a: unknown[]) => getAgentInterface(...a),
  getAgentInterfaceHistory: (...a: unknown[]) => getAgentInterfaceHistory(...a),
  saveAgentInterface: (...a: unknown[]) => saveAgentInterface(...a),
  unpublishAgentInterface: (...a: unknown[]) => unpublishAgentInterface(...a),
  setCapabilityPhi: (...a: unknown[]) => setCapabilityPhi(...a),
}))

const { AgentInterfaceView, declaresPhi } = await import('./AgentInterfaceView')
const realApi = await vi.importActual<typeof import('@/api/agents')>('@/api/agents')

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

const PHI_SOURCE = `capabilities:
  data:
    callers: [contact]
    sites: [connect-labs]
    data: phi   # vouched
    tools: [Read]
`
const PHI_ON = {
  interface: { capabilities: { data: { callers: ['contact'], sites: ['connect-labs'], data: 'phi', tools: ['Read'] } } },
  source: PHI_SOURCE, published_at: '2026-10-22T09:00:00Z', published_by_email: 'jj@dimagi.com',
}
const change = (id: number, operation: string, body: unknown, via = 'ui') => ({
  id, created_at: '2026-10-22T09:00:00Z', workspace: 'labs', actor_email: 'jj@dimagi.com', via,
  credential: {}, client: '', mcp_tool: '', request_id: '', operation, method: 'PUT',
  path: '/api/agents/labsdata/interface', target: 'labsdata', summary: '', request: { path: {}, body }, changes: [],
})

beforeEach(() => {
  getAgentInterfaceHistory.mockResolvedValue([])
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  setStepUpHandler(null)
})

describe('AgentInterfaceView', () => {
  it('says plainly when nothing is published, and offers no edit to a non-admin', async () => {
    getAgentInterface.mockResolvedValue({ interface: {}, source: '', published_at: null, published_by_email: null })
    render(<AgentInterfaceView agentSlug="ace" />)
    expect((await screen.findByTestId('interface-none')).textContent).toMatch(/No outside callers. Only workspace members can use/)
    expect(screen.queryByRole('button', { name: /Set up|Edit/ })).toBeNull()
  })

  it('shows domain-wide access, each capability and its inputs, and no MCP tool', async () => {
    getAgentInterface.mockResolvedValue(PUBLISHED)
    render(<AgentInterfaceView agentSlug="ace" />)
    expect((await screen.findByTestId('interface-full')).textContent).toContain('contact@dimagi.com:verified')
    const row = screen.getByTestId('interface-capability')
    expect(row.textContent).toContain('Inputs: opportunity_id: integer')
    // Agents are not tools on canopy's MCP: you install the agent's plugin.
    expect(row.textContent).not.toContain('ace__ask')
    expect(row.textContent).toContain('canopy email read --repo . {thread_id}')
  })

  it("shows the server's warnings about rules no mail can satisfy", async () => {
    const warning = 'member@nodmarc.org: mail from nodmarc.org can never prove who sent it'
    getAgentInterface.mockResolvedValue({ ...PUBLISHED, warnings: [warning] })
    render(<AgentInterfaceView agentSlug="ace" />)
    expect((await screen.findByTestId('interface-warning')).textContent).toBe(warning)
  })

  it('shows no warning when there is none', async () => {
    getAgentInterface.mockResolvedValue(PUBLISHED)
    render(<AgentInterfaceView agentSlug="ace" />)
    await screen.findByTestId('interface-full')
    expect(screen.queryByTestId('interface-warning')).toBeNull()
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

describe('declaresPhi', () => {
  it('sees data: phi in block or flow form, never in a comment', () => {
    expect(declaresPhi(PHI_SOURCE)).toBe(true)
    expect(declaresPhi('capabilities: {data: {sites: [x], data: phi}}')).toBe(true)
    expect(declaresPhi('{"capabilities": {"data": {"data": "phi"}}}')).toBe(true)
    expect(declaresPhi('capabilities:\n  ask:\n    # data: phi later\n    callers: [member]\n')).toBe(false)
    expect(declaresPhi('capabilities:\n  data:\n    sites: [x]\n')).toBe(false)
  })
})

describe('AgentInterfaceView — the PHI-safe setting', () => {
  it('says what turning it on vouches for, and saves it without a passkey', async () => {
    getAgentInterface.mockResolvedValue({ interface: {}, source: '', published_at: null, published_by_email: null })
    // The real client: one PUT, no passkey prompt.
    saveAgentInterface.mockImplementation(realApi.saveAgentInterface)
    const bodies: unknown[] = []
    vi.stubGlobal('fetch', vi.fn(async (r: Request) => {
      bodies.push(await r.clone().json())
      return new Response(JSON.stringify(PHI_ON), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    const prompt = vi.fn().mockResolvedValue(undefined)
    setStepUpHandler(prompt)

    render(<AgentInterfaceView agentSlug="labsdata" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Set up' }))
    expect(screen.queryByTestId('phi-on')).toBeNull()
    fireEvent.change(screen.getByLabelText('Interface (YAML)'), { target: { value: PHI_SOURCE } })
    expect((await screen.findByTestId('phi-on')).textContent).toMatch(/you vouch that it reads a host's data only/)
    expect(screen.queryByLabelText(/Commit/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Save and turn on PHI-safe' }))
    expect((await screen.findByTestId('phi-safe')).textContent).toBe('PHI-safe')
    expect(prompt).not.toHaveBeenCalled()
    expect(bodies).toEqual([{ source: PHI_SOURCE }])
  })

  it('marks each PHI-safe capability in the table', async () => {
    getAgentInterface.mockResolvedValue({
      ...PHI_ON,
      interface: { capabilities: { ...PHI_ON.interface.capabilities, ask: { callers: ['member'], tools: ['Read'] } } },
    })
    render(<AgentInterfaceView agentSlug="labsdata" />)
    const rows = await screen.findAllByTestId('interface-capability')
    const byName = Object.fromEntries(rows.map((r) => [r.querySelector('.font-mono')?.textContent, r.textContent]))
    expect(byName.data).toContain('PHI-safe')
    expect(byName.ask).not.toContain('PHI-safe')
  })

  it('turns it off by taking data: phi out and saving — the same save, said plainly', async () => {
    getAgentInterface.mockResolvedValue(PHI_ON)
    saveAgentInterface.mockResolvedValue({ ...PHI_ON, interface: { capabilities: { data: { callers: ['contact'], sites: ['connect-labs'], tools: ['Read'] } } } })
    render(<AgentInterfaceView agentSlug="labsdata" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Turn off PHI-safe' }))
    const box = screen.getByLabelText('Interface (YAML)') as HTMLTextAreaElement
    expect(declaresPhi(box.value)).toBe(false)
    expect(box.value).toContain('sites: [connect-labs]')
    expect(screen.getByTestId('phi-off').textContent).toMatch(/turns PHI-safe off for data/)
    fireEvent.click(screen.getByRole('button', { name: 'Save and turn off PHI-safe' }))
    await waitFor(() => expect(saveAgentInterface).toHaveBeenCalledWith('labsdata', box.value))
    await waitFor(() => expect(screen.queryByTestId('phi-safe')).toBeNull())
  })

  it('shows the audited history: who, through what, and whether PHI-safe was on after', async () => {
    getAgentInterface.mockResolvedValue(PHI_ON)
    getAgentInterfaceHistory.mockResolvedValue([
      change(3, 'unpublish_interface', {}),
      change(2, 'publish_interface', { source: PHI_SOURCE }),
      change(1, 'publish_interface', { source: 'capabilities:\n  ask:\n    callers: [member]\n' }, 'mcp'),
    ])
    render(<AgentInterfaceView agentSlug="labsdata" />)
    const rows = await screen.findAllByTestId('interface-change')
    expect(rows.map((r) => r.lastElementChild?.textContent)).toEqual(['off', 'on', 'off'])
    expect(rows[0].textContent).toContain('Unpublished')
    expect(rows[2].textContent).toContain('mcp')
    expect(getAgentInterfaceHistory).toHaveBeenCalledWith('labsdata')
  })

  it('the switch turns one capability on at once, like any other edit', async () => {
    const off = { ...PHI_ON, interface: { capabilities: { data: { callers: ['contact'], sites: ['connect-labs'], tools: ['Read'] } } } }
    getAgentInterface.mockResolvedValue(off)
    setCapabilityPhi.mockResolvedValue(PHI_ON)
    render(<AgentInterfaceView agentSlug="labsdata" canEdit />)
    const sw = await screen.findByRole('switch', { name: 'PHI-safe for data' })
    expect(sw.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(sw)
    await waitFor(() => expect(setCapabilityPhi).toHaveBeenCalledWith('labsdata', 'data', true))
    expect((await screen.findByTestId('phi-safe')).textContent).toBe('PHI-safe')
  })

  it('the switch turns one capability off at once, and is not offered to a non-admin', async () => {
    getAgentInterface.mockResolvedValue(PHI_ON)
    setCapabilityPhi.mockResolvedValue({ ...PHI_ON, interface: { capabilities: { data: { callers: ['contact'], sites: ['connect-labs'], tools: ['Read'] } } } })
    const { unmount } = render(<AgentInterfaceView agentSlug="labsdata" />)
    await screen.findByTestId('phi-safe')
    expect(screen.queryByRole('switch')).toBeNull()
    unmount()
    render(<AgentInterfaceView agentSlug="labsdata" canEdit />)
    const sw = await screen.findByRole('switch', { name: 'PHI-safe for data' })
    expect(sw.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(sw)
    await waitFor(() => expect(setCapabilityPhi).toHaveBeenCalledWith('labsdata', 'data', false))
    await waitFor(() => expect(screen.queryByTestId('phi-safe')).toBeNull())
  })

  it("names a switch change in the history, with PHI-safe after from what it recorded", async () => {
    getAgentInterface.mockResolvedValue(PHI_ON)
    getAgentInterfaceHistory.mockResolvedValue([
      { ...change(2, 'set_capability_phi', { phi_safe: false }), target: 'labsdata / data',
        changes: [{ scope: 'interface:labsdata/data', before: { phi_capabilities: ['data'] }, after: { phi_capabilities: [] } }] },
      { ...change(1, 'set_capability_phi', { phi_safe: true }), target: 'labsdata / data',
        changes: [{ scope: 'interface:labsdata/data', before: { phi_capabilities: [] }, after: { phi_capabilities: ['data'] } }] },
    ])
    render(<AgentInterfaceView agentSlug="labsdata" />)
    const rows = await screen.findAllByTestId('interface-change')
    expect(rows.map((r) => r.lastElementChild?.textContent)).toEqual(['off', 'on'])
    expect(rows[0].textContent).toContain('PHI-safe off for data')
    expect(rows[1].textContent).toContain('PHI-safe on for data')
  })
})
