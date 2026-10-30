// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setStepUpHandler } from '@/auth/stepUp'

import ACE from './aceInterface.fixture.json'

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
const saveAgentInterfaceModel = vi.fn()
const validateAgentInterface = vi.fn()
const resetAgentInterface = vi.fn()
const setCapabilitySensitive = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getAgentInterface: (...a: unknown[]) => getAgentInterface(...a),
  getAgentInterfaceHistory: (...a: unknown[]) => getAgentInterfaceHistory(...a),
  saveAgentInterface: (...a: unknown[]) => saveAgentInterface(...a),
  saveAgentInterfaceModel: (...a: unknown[]) => saveAgentInterfaceModel(...a),
  validateAgentInterface: (...a: unknown[]) => validateAgentInterface(...a),
  resetAgentInterface: (...a: unknown[]) => resetAgentInterface(...a),
  setCapabilitySensitive: (...a: unknown[]) => setCapabilitySensitive(...a),
}))

const { AgentInterfaceView } = await import('./AgentInterfaceView')

const PUBLISHED = {
  interface: {
    capabilities: { ask: {
      description: 'Ask ACE.', role: 'viewer',
      tools: ['Read'], bash: ['canopy email read --repo . {thread_id}'], input: { opportunity_id: 'integer' },
    } },
  },
  source: 'capabilities:\n  ask:\n    role: viewer  # comment kept\n',
  yaml: 'capabilities:\n  ask:\n    role: viewer  # comment kept\n',
  is_default: false,
  published_at: '2026-09-21T12:00:00Z', published_by_email: 'op@dimagi.com',
}

const SENSITIVE_SOURCE = `capabilities:
  data:
    role: user
    sites: [connect-labs]
    data: sensitive   # vouched
    tools: [Read]
`
const DEFAULT = {
  interface: { version: 1, capabilities: {} },
  source: '', yaml: '', is_default: true, published_at: null, published_by_email: null,
}
// The server's parser, as the form meets it: valid drafts come back with their model and YAML.
const accepts = (draft: { interface?: unknown; source?: string }) => ({
  errors: [],
  interface: draft.interface ?? {}, yaml: draft.source ?? 'yaml: of the draft\n',
})

const SENSITIVE_ON = {
  interface: { capabilities: { data: { role: 'user', sites: ['connect-labs'], data: 'sensitive', tools: ['Read'] } } },
  source: SENSITIVE_SOURCE, published_at: '2026-10-22T09:00:00Z', published_by_email: 'jj@dimagi.com',
}
const change = (id: number, operation: string, body: unknown, via = 'ui') => ({
  id, created_at: '2026-10-22T09:00:00Z', workspace: 'labs', actor_email: 'jj@dimagi.com', via,
  credential: {}, client: '', mcp_tool: '', request_id: '', operation, method: 'PUT',
  path: '/api/agents/labsdata/interface', target: 'labsdata', summary: '', request: { path: {}, body }, changes: [],
})

beforeEach(() => {
  getAgentInterfaceHistory.mockResolvedValue([])
  validateAgentInterface.mockImplementation(async (_slug: string, draft: { interface?: unknown; source?: string }) => accepts(draft))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  setStepUpHandler(null)
})

describe('AgentInterfaceView', () => {
  it('says an agent on the default offers no capabilities, and offers no edit to a non-admin', async () => {
    getAgentInterface.mockResolvedValue(DEFAULT)
    render(<AgentInterfaceView agentSlug="echo" agentName="Echo" />)
    expect((await screen.findByTestId('interface-default')).textContent).toMatch(/offers no capabilities \(default\).*Access setting/)
    expect(screen.queryByRole('button', { name: /Add capabilities|Edit/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Reset/ })).toBeNull()
  })

  it('shows each capability with its lowest role and inputs, no access summary, and no MCP tool', async () => {
    getAgentInterface.mockResolvedValue(PUBLISHED)
    render(<AgentInterfaceView agentSlug="ace" />)
    const row = await screen.findByTestId('interface-capability')
    expect(screen.queryByTestId('summary-line')).toBeNull()
    expect(screen.queryByTestId('access-summary')).toBeNull()
    expect(screen.queryByTestId('interface-warning')).toBeNull()
    expect(row.textContent).toContain('Viewer')
    expect(row.textContent).toContain('Inputs: opportunity_id: integer')
    expect(row.textContent).not.toContain('ace__ask')
    expect(row.textContent).toContain('canopy email read --repo . {thread_id}')
    expect(screen.getByText(/set on the Access setting/)).toBeTruthy()
  })

  it('a capability with no role reads as User, the default', async () => {
    getAgentInterface.mockResolvedValue({ ...PUBLISHED, interface: { capabilities: { act: { tools: ['Read'] } } } })
    render(<AgentInterfaceView agentSlug="ace" />)
    expect((await screen.findByTestId('interface-capability')).textContent).toContain('User')
  })

  it('an admin adds the starter ask capability for colleagues and saves it as a viewer capability', async () => {
    getAgentInterface.mockResolvedValue(DEFAULT)
    saveAgentInterfaceModel.mockImplementation(async (_s: string, m: unknown) => ({ ...PUBLISHED, interface: m }))
    render(<AgentInterfaceView agentSlug="echo" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add capabilities' }))
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Add an ask capability for colleagues' }))
    expect((await screen.findAllByTestId('capability-card')).length).toBe(1)
    expect((screen.getByLabelText('ask role') as HTMLSelectElement).value).toBe('viewer')
    await waitFor(() => expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveAgentInterfaceModel).toHaveBeenCalled())
    const sent = saveAgentInterfaceModel.mock.calls[0][1]
    expect(sent.capabilities.ask.role).toBe('viewer')
    expect(sent.capabilities.ask).not.toHaveProperty('callers')
    expect(sent).not.toHaveProperty('full')
    expect(await screen.findByTestId('interface-capability')).toBeTruthy()
  })

  it('has no callers, whole-agent or editors controls, and points to the Access setting', async () => {
    getAgentInterface.mockResolvedValue(PUBLISHED)
    render(<AgentInterfaceView agentSlug="ace" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    await screen.findByLabelText('ask role')
    expect(screen.queryByRole('button', { name: 'Add who' })).toBeNull()
    expect(screen.queryByTestId('caller-row')).toBeNull()
    expect(screen.queryByText(/Whole agent/)).toBeNull()
    expect(screen.queryByTestId('draft-summary')).toBeNull()
    expect(screen.getAllByText(/Access setting/).length).toBeGreaterThan(0)
  })

  it('changes a capability to a user capability with the role choice, and sends that role', async () => {
    getAgentInterface.mockResolvedValue(PUBLISHED)
    saveAgentInterfaceModel.mockImplementation(async (_s: string, m: unknown) => ({ ...PUBLISHED, interface: m }))
    render(<AgentInterfaceView agentSlug="ace" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    const role = (await screen.findByLabelText('ask role')) as HTMLSelectElement
    expect(role.value).toBe('viewer')
    expect(Array.from(role.options).map((o) => [o.value, o.text])).toEqual([
      ['viewer', 'Viewer — anyone the access list gives at least viewer'],
      ['user', 'User — only users and above'],
    ])
    fireEvent.change(role, { target: { value: 'user' } })
    await waitFor(() =>
      expect(validateAgentInterface).toHaveBeenLastCalledWith('ace', expect.objectContaining({
        interface: expect.objectContaining({ capabilities: expect.objectContaining({ ask: expect.objectContaining({ role: 'user' }) }) }),
      })))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveAgentInterfaceModel).toHaveBeenCalled())
    expect(saveAgentInterfaceModel.mock.calls[0][1].capabilities.ask.role).toBe('user')
  })

  it('a capability with no role shows User in the form, and a site capability starts as a viewer capability', async () => {
    getAgentInterface.mockResolvedValue({ ...DEFAULT, interface: { capabilities: { act: { tools: ['Read'] } } }, is_default: false })
    render(<AgentInterfaceView agentSlug="ace" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    expect(((await screen.findByLabelText('act role')) as HTMLSelectElement).value).toBe('user')
    fireEvent.click(screen.getByRole('button', { name: 'Add a connect-labs site capability' }))
    expect(((await screen.findByLabelText('connect role')) as HTMLSelectElement).value).toBe('viewer')
  })

  it("shows the server parser's reason next to the field it is about, and will not save", async () => {
    getAgentInterface.mockResolvedValue(PUBLISHED)
    validateAgentInterface.mockResolvedValue({
      errors: [{ path: 'ask.tools', message: 'ask.tools entries must be non-empty strings' }],
      interface: null, yaml: '',
    })
    render(<AgentInterfaceView agentSlug="ace" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    const tools = await screen.findByLabelText('ask tools')
    fireEvent.change(tools, { target: { value: 'Read\nGrep' } })
    const err = await screen.findByTestId('field-error')
    expect(err.textContent).toMatch(/ask.tools entries must be non-empty/)
    expect(tools.closest('label')?.contains(err)).toBe(true)
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it("ACE's published interface survives load → save in the form unchanged", async () => {
    getAgentInterface.mockResolvedValue({ ...DEFAULT, interface: ACE, is_default: false })
    saveAgentInterfaceModel.mockImplementation(async (_s: string, m: unknown) => ({ ...DEFAULT, interface: m, is_default: false }))
    render(<AgentInterfaceView agentSlug="ace" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    expect((await screen.findAllByTestId('capability-card')).length).toBe(3)
    // Touch a field and put it back: the form has now round-tripped every card.
    const tools = screen.getByLabelText('canopy tools') as HTMLTextAreaElement
    const before = tools.value
    fireEvent.change(tools, { target: { value: `${before}\nExtra` } })
    fireEvent.change(tools, { target: { value: before } })
    await waitFor(() => expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveAgentInterfaceModel).toHaveBeenCalled())
    expect(saveAgentInterfaceModel.mock.calls[0][1]).toEqual(ACE)
  })

  it('moves between the form and the YAML through the server, and saves the YAML as written', async () => {
    getAgentInterface.mockResolvedValue(PUBLISHED)
    saveAgentInterface.mockResolvedValue({ ...PUBLISHED, source: 'capabilities:\n  ask:\n    role: user\n' })
    render(<AgentInterfaceView agentSlug="ace" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.click(await screen.findByRole('tab', { name: 'Advanced: YAML' }))
    const box = (await screen.findByLabelText('Interface (YAML)')) as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'capabilities:\n  ask:\n    role: user\n' } })
    await waitFor(() => expect(validateAgentInterface).toHaveBeenLastCalledWith('ace', { source: 'capabilities:\n  ask:\n    role: user\n' }))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveAgentInterface).toHaveBeenCalledWith('ace', 'capabilities:\n  ask:\n    role: user\n'))
  })

  it('"Reset to default" replaces unpublish and returns the default', async () => {
    getAgentInterface.mockResolvedValue(PUBLISHED)
    resetAgentInterface.mockResolvedValue(DEFAULT)
    render(<AgentInterfaceView agentSlug="ace" agentName="ACE" canEdit />)
    expect(screen.queryByRole('button', { name: /Unpublish/ })).toBeNull()
    fireEvent.click(await screen.findByRole('button', { name: 'Reset to default' }))
    expect((await screen.findByTestId('interface-default')).textContent).toMatch(/offers no capabilities \(default\)/)
  })
})

describe('AgentInterfaceView — the sensitive-data setting', () => {
  it('the form checkbox says what turning it on vouches for', async () => {
    getAgentInterface.mockResolvedValue(DEFAULT)
    render(<AgentInterfaceView agentSlug="labsdata" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add capabilities' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add a connect-labs site capability' }))
    expect(screen.queryByTestId('sensitive-on')).toBeNull()
    fireEvent.click(await screen.findByLabelText('connect handles sensitive data'))
    expect((await screen.findByTestId('sensitive-on')).textContent).toMatch(/you vouch that it reads a host's data only/)
  })

  it('unchecking it says saving turns sensitive data off', async () => {
    getAgentInterface.mockResolvedValue(SENSITIVE_ON)
    render(<AgentInterfaceView agentSlug="labsdata" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.click(await screen.findByLabelText('data handles sensitive data'))
    expect((await screen.findByTestId('sensitive-off')).textContent).toMatch(/turns it off for data/)
  })

  it('marks each capability that handles sensitive data in the table', async () => {
    getAgentInterface.mockResolvedValue({
      ...SENSITIVE_ON,
      interface: { capabilities: { ...SENSITIVE_ON.interface.capabilities, ask: { role: 'viewer', tools: ['Read'] } } },
    })
    render(<AgentInterfaceView agentSlug="labsdata" />)
    const rows = await screen.findAllByTestId('interface-capability')
    const byName = Object.fromEntries(rows.map((r) => [r.querySelector('.font-mono')?.textContent, r.textContent]))
    expect(byName.data).toContain('Handles sensitive data')
    expect(byName.ask).not.toContain('Handles sensitive data')
  })

  it('shows the audited history: who, through what, and whether sensitive data was on after', async () => {
    getAgentInterface.mockResolvedValue(SENSITIVE_ON)
    getAgentInterfaceHistory.mockResolvedValue([
      change(3, 'reset_interface', {}),
      change(2, 'publish_interface', { source: SENSITIVE_SOURCE }),
      change(1, 'publish_interface', { source: 'capabilities:\n  ask:\n    role: viewer\n' }, 'mcp'),
    ])
    render(<AgentInterfaceView agentSlug="labsdata" />)
    const rows = await screen.findAllByTestId('interface-change')
    expect(rows.map((r) => r.lastElementChild?.textContent)).toEqual(['off', 'on', 'off'])
    expect(rows[0].textContent).toContain('Reset to default')
    expect(rows[2].textContent).toContain('mcp')
    expect(getAgentInterfaceHistory).toHaveBeenCalledWith('labsdata')
  })

  it('the switch turns one capability on at once, like any other edit', async () => {
    const off = { ...SENSITIVE_ON, interface: { capabilities: { data: { role: 'user', sites: ['connect-labs'], tools: ['Read'] } } } }
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
    setCapabilitySensitive.mockResolvedValue({ ...SENSITIVE_ON, interface: { capabilities: { data: { role: 'user', sites: ['connect-labs'], tools: ['Read'] } } } })
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
