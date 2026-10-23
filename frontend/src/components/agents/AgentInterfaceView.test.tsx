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
const setCapabilitySensitive = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getAgentInterface: (...a: unknown[]) => getAgentInterface(...a),
  getAgentInterfaceHistory: (...a: unknown[]) => getAgentInterfaceHistory(...a),
  saveAgentInterface: (...a: unknown[]) => saveAgentInterface(...a),
  unpublishAgentInterface: (...a: unknown[]) => unpublishAgentInterface(...a),
  setCapabilitySensitive: (...a: unknown[]) => setCapabilitySensitive(...a),
}))

const { AgentInterfaceView, declaresSensitive } = await import('./AgentInterfaceView')
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

const SENSITIVE_SOURCE = `capabilities:
  data:
    callers: [contact]
    sites: [connect-labs]
    data: sensitive   # vouched
    tools: [Read]
`
const SENSITIVE_ON = {
  interface: { capabilities: { data: { callers: ['contact'], sites: ['connect-labs'], data: 'sensitive', tools: ['Read'] } } },
  source: SENSITIVE_SOURCE, published_at: '2026-10-22T09:00:00Z', published_by_email: 'jj@dimagi.com',
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

describe('declaresSensitive', () => {
  it('sees data: sensitive in block or flow form, never in a comment', () => {
    expect(declaresSensitive(SENSITIVE_SOURCE)).toBe(true)
    expect(declaresSensitive('capabilities: {data: {sites: [x], data: sensitive}}')).toBe(true)
    expect(declaresSensitive('{"capabilities": {"data": {"data": "sensitive"}}}')).toBe(true)
    expect(declaresSensitive('capabilities:\n  ask:\n    # data: sensitive later\n    callers: [member]\n')).toBe(false)
    expect(declaresSensitive('capabilities:\n  data:\n    sites: [x]\n')).toBe(false)
  })
})

describe('AgentInterfaceView — the sensitive-data setting', () => {
  it('says what turning it on vouches for, and saves it without a passkey', async () => {
    getAgentInterface.mockResolvedValue({ interface: {}, source: '', published_at: null, published_by_email: null })
    // The real client: one PUT, no passkey prompt.
    saveAgentInterface.mockImplementation(realApi.saveAgentInterface)
    const bodies: unknown[] = []
    vi.stubGlobal('fetch', vi.fn(async (r: Request) => {
      bodies.push(await r.clone().json())
      return new Response(JSON.stringify(SENSITIVE_ON), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    const prompt = vi.fn().mockResolvedValue(undefined)
    setStepUpHandler(prompt)

    render(<AgentInterfaceView agentSlug="labsdata" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Set up' }))
    expect(screen.queryByTestId('sensitive-on')).toBeNull()
    fireEvent.change(screen.getByLabelText('Interface (YAML)'), { target: { value: SENSITIVE_SOURCE } })
    expect((await screen.findByTestId('sensitive-on')).textContent).toMatch(/you vouch that it reads a host's data only/)
    expect(screen.queryByLabelText(/Commit/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Save and turn on sensitive data' }))
    expect((await screen.findByTestId('handles-sensitive')).textContent).toBe('Handles sensitive data')
    expect(prompt).not.toHaveBeenCalled()
    expect(bodies).toEqual([{ source: SENSITIVE_SOURCE }])
  })

  it('marks each capability that handles sensitive data in the table', async () => {
    getAgentInterface.mockResolvedValue({
      ...SENSITIVE_ON,
      interface: { capabilities: { ...SENSITIVE_ON.interface.capabilities, ask: { callers: ['member'], tools: ['Read'] } } },
    })
    render(<AgentInterfaceView agentSlug="labsdata" />)
    const rows = await screen.findAllByTestId('interface-capability')
    const byName = Object.fromEntries(rows.map((r) => [r.querySelector('.font-mono')?.textContent, r.textContent]))
    expect(byName.data).toContain('Handles sensitive data')
    expect(byName.ask).not.toContain('Handles sensitive data')
  })

  it('turns it off by taking data: sensitive out and saving — the same save, said plainly', async () => {
    getAgentInterface.mockResolvedValue(SENSITIVE_ON)
    saveAgentInterface.mockResolvedValue({ ...SENSITIVE_ON, interface: { capabilities: { data: { callers: ['contact'], sites: ['connect-labs'], tools: ['Read'] } } } })
    render(<AgentInterfaceView agentSlug="labsdata" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Turn off sensitive data' }))
    const box = screen.getByLabelText('Interface (YAML)') as HTMLTextAreaElement
    expect(declaresSensitive(box.value)).toBe(false)
    expect(box.value).toContain('sites: [connect-labs]')
    expect(screen.getByTestId('sensitive-off').textContent).toMatch(/turns sensitive data off for data/)
    fireEvent.click(screen.getByRole('button', { name: 'Save and turn off sensitive data' }))
    await waitFor(() => expect(saveAgentInterface).toHaveBeenCalledWith('labsdata', box.value))
    await waitFor(() => expect(screen.queryByTestId('handles-sensitive')).toBeNull())
  })

  it('shows the audited history: who, through what, and whether sensitive data was on after', async () => {
    getAgentInterface.mockResolvedValue(SENSITIVE_ON)
    getAgentInterfaceHistory.mockResolvedValue([
      change(3, 'unpublish_interface', {}),
      change(2, 'publish_interface', { source: SENSITIVE_SOURCE }),
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
    const off = { ...SENSITIVE_ON, interface: { capabilities: { data: { callers: ['contact'], sites: ['connect-labs'], tools: ['Read'] } } } }
    getAgentInterface.mockResolvedValue(off)
    setCapabilitySensitive.mockResolvedValue(SENSITIVE_ON)
    render(<AgentInterfaceView agentSlug="labsdata" canEdit />)
    const sw = await screen.findByRole('switch', { name: 'Handles sensitive data for data' })
    expect(sw.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(sw)
    await waitFor(() => expect(setCapabilitySensitive).toHaveBeenCalledWith('labsdata', 'data', true))
    expect((await screen.findByTestId('handles-sensitive')).textContent).toBe('Handles sensitive data')
  })

  it('the switch turns one capability off at once, and is not offered to a non-admin', async () => {
    getAgentInterface.mockResolvedValue(SENSITIVE_ON)
    setCapabilitySensitive.mockResolvedValue({ ...SENSITIVE_ON, interface: { capabilities: { data: { callers: ['contact'], sites: ['connect-labs'], tools: ['Read'] } } } })
    const { unmount } = render(<AgentInterfaceView agentSlug="labsdata" />)
    await screen.findByTestId('handles-sensitive')
    expect(screen.queryByRole('switch')).toBeNull()
    unmount()
    render(<AgentInterfaceView agentSlug="labsdata" canEdit />)
    const sw = await screen.findByRole('switch', { name: 'Handles sensitive data for data' })
    expect(sw.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(sw)
    await waitFor(() => expect(setCapabilitySensitive).toHaveBeenCalledWith('labsdata', 'data', false))
    await waitFor(() => expect(screen.queryByTestId('handles-sensitive')).toBeNull())
  })

  it("names a switch change in the history, with sensitive data after from what it recorded", async () => {
    getAgentInterface.mockResolvedValue(SENSITIVE_ON)
    getAgentInterfaceHistory.mockResolvedValue([
      { ...change(2, 'set_capability_sensitive', { handles_sensitive: false }), target: 'labsdata / data',
        changes: [{ scope: 'interface:labsdata/data', before: { sensitive_capabilities: ['data'] }, after: { sensitive_capabilities: [] } }] },
      { ...change(1, 'set_capability_sensitive', { handles_sensitive: true }), target: 'labsdata / data',
        changes: [{ scope: 'interface:labsdata/data', before: { sensitive_capabilities: [] }, after: { sensitive_capabilities: ['data'] } }] },
    ])
    render(<AgentInterfaceView agentSlug="labsdata" />)
    const rows = await screen.findAllByTestId('interface-change')
    expect(rows.map((r) => r.lastElementChild?.textContent)).toEqual(['off', 'on'])
    expect(rows[0].textContent).toContain('Sensitive data off for data')
    expect(rows[1].textContent).toContain('Sensitive data on for data')
  })
})
