// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

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
const getAgentRepoHead = vi.fn()
const saveAgentInterface = vi.fn()
const unpublishAgentInterface = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getAgentInterface: (...a: unknown[]) => getAgentInterface(...a),
  getAgentRepoHead: (...a: unknown[]) => getAgentRepoHead(...a),
  saveAgentInterface: (...a: unknown[]) => saveAgentInterface(...a),
  unpublishAgentInterface: (...a: unknown[]) => unpublishAgentInterface(...a),
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

const SHA = 'a'.repeat(40)
const HEAD = 'b'.repeat(40)
const PHI_SOURCE = `capabilities:
  data:
    callers: [contact]
    sites: [connect-labs]
    data: phi   # vouched
    tools: [Read]
`
const DECLARATION = {
  capability: 'data', data: 'phi', sha: SHA, declared_by_email: 'jj@dimagi.com',
  declared_at: '2026-10-22T09:00:00Z', commit_url: `https://github.com/dimagi-internal/labsdata/commit/${SHA}`,
  last_run_sha: SHA, last_run_at: '2026-10-22T10:00:00Z',
}
const DECLARED = {
  interface: { capabilities: { data: { callers: ['contact'], sites: ['connect-labs'], data: 'phi', tools: ['Read'] } } },
  source: PHI_SOURCE, published_at: '2026-10-22T09:00:00Z', published_by_email: 'jj@dimagi.com',
  declarations: [DECLARATION],
}
const headAt = (sha: string) => ({
  repo: 'dimagi-internal/labsdata', ref: 'main', sha, detail: '',
  commit_url: sha ? `https://github.com/dimagi-internal/labsdata/commit/${sha}` : '',
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

describe('AgentInterfaceView — the PHI-safe declaration', () => {
  it('asks for the commit when the YAML says data: phi, offers the repo head, and steps up to save it', async () => {
    getAgentInterface.mockResolvedValue({ interface: {}, source: '', published_at: null, published_by_email: null, declarations: [] })
    getAgentRepoHead.mockResolvedValue(headAt(HEAD))
    // The real client: the first PUT is refused for a passkey, the API client
    // prompts, and the retry carries the same body.
    saveAgentInterface.mockImplementation(realApi.saveAgentInterface)
    const bodies: unknown[] = []
    vi.stubGlobal('fetch', vi.fn(async (r: Request) => {
      bodies.push(await r.clone().json())
      return bodies.length === 1
        ? new Response(JSON.stringify({ error: 'step_up_required', has_passkey: true, status: 403, detail: 'Confirm it’s you' }),
          { status: 403, headers: { 'Content-Type': 'application/problem+json' } })
        : new Response(JSON.stringify({ ...DECLARED, declarations: [{ ...DECLARATION, sha: HEAD, last_run_sha: null, last_run_at: null }] }),
          { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    const prompt = vi.fn().mockResolvedValue(undefined)
    setStepUpHandler(prompt)

    render(<AgentInterfaceView agentSlug="labsdata" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Set up' }))
    expect(screen.queryByTestId('phi-declare')).toBeNull()
    fireEvent.change(screen.getByLabelText('Interface (YAML)'), { target: { value: PHI_SOURCE } })
    expect((await screen.findByTestId('phi-declare')).textContent).toMatch(/you vouch that, at this commit/)
    const sha = screen.getByLabelText('Commit the declaration covers') as HTMLInputElement
    await waitFor(() => expect(sha.value).toBe(HEAD))
    expect(screen.getByTestId('phi-declare-head').textContent).toContain('Current head of dimagi-internal/labsdata@main')
    expect(getAgentRepoHead).toHaveBeenCalledWith('labsdata')

    fireEvent.click(screen.getByRole('button', { name: 'Save and declare' }))
    await waitFor(() => expect(prompt).toHaveBeenCalledWith({ hasPasskey: true, detail: 'Confirm it’s you' }))
    const row = await screen.findByTestId('interface-declaration')
    expect(row.textContent).toContain(HEAD.slice(0, 12))
    expect(bodies).toEqual([{ source: PHI_SOURCE, declare_sha: HEAD }, { source: PHI_SOURCE, declare_sha: HEAD }])
  })

  it('takes a typed commit when canopy cannot read the head, and refuses one that is not 40 hex', async () => {
    getAgentInterface.mockResolvedValue({ interface: {}, source: '', published_at: null, published_by_email: null, declarations: [] })
    getAgentRepoHead.mockResolvedValue({ ...headAt(''), detail: 'the agent has no GitHub identity to read its repository with' })
    saveAgentInterface.mockResolvedValue(DECLARED)
    render(<AgentInterfaceView agentSlug="labsdata" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Set up' }))
    fireEvent.change(screen.getByLabelText('Interface (YAML)'), { target: { value: PHI_SOURCE } })
    expect((await screen.findByTestId('phi-declare-head')).textContent).toMatch(/could not read .*no GitHub identity.*paste the commit/)
    const save = screen.getByRole('button', { name: 'Save and declare' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Commit the declaration covers'), { target: { value: 'main' } })
    expect(screen.getByRole('alert').textContent).toMatch(/40-character commit SHA/)
    expect(save.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Commit the declaration covers'), { target: { value: SHA } })
    expect(save.disabled).toBe(false)
    fireEvent.click(save)
    await waitFor(() => expect(saveAgentInterface).toHaveBeenCalledWith('labsdata', PHI_SOURCE, SHA))
  })

  it('shows each declaration in force: capability, data, linked commit, who and when', async () => {
    getAgentInterface.mockResolvedValue(DECLARED)
    render(<AgentInterfaceView agentSlug="labsdata" />)
    const row = await screen.findByTestId('interface-declaration')
    expect(row.textContent).toContain('data: phi')
    expect(row.textContent).toContain('jj@dimagi.com')
    const link = screen.getByRole('link', { name: SHA.slice(0, 12) }) as HTMLAnchorElement
    expect(link.href).toBe(DECLARATION.commit_url)
    expect(screen.queryByTestId('declaration-mismatch')).toBeNull()
    // A member who is not an admin is never shown the repo head (it is read as the agent).
    expect(getAgentRepoHead).not.toHaveBeenCalled()
  })

  it('warns when the last turn ran a commit the declaration does not cover', async () => {
    getAgentInterface.mockResolvedValue({ ...DECLARED, declarations: [{ ...DECLARATION, last_run_sha: HEAD }] })
    render(<AgentInterfaceView agentSlug="labsdata" />)
    const warn = await screen.findByTestId('declaration-mismatch')
    expect(warn.textContent).toContain(`ran ${HEAD.slice(0, 12)}, not the declared ${SHA.slice(0, 12)}`)
    expect(warn.textContent).toMatch(/hosts\s+withheld their PHI/)
  })

  it('tells an admin the repo has moved on, and re-declares at its head in one click', async () => {
    getAgentInterface.mockResolvedValue(DECLARED)
    getAgentRepoHead.mockResolvedValue(headAt(HEAD))
    render(<AgentInterfaceView agentSlug="labsdata" canEdit />)
    const behind = await screen.findByTestId('declaration-behind')
    expect(behind.textContent).toMatch(/keep running the declared commit/)
    fireEvent.click(screen.getByRole('button', { name: `Declare ${HEAD.slice(0, 12)}` }))
    expect((screen.getByLabelText('Commit the declaration covers') as HTMLInputElement).value).toBe(HEAD)
    expect((screen.getByLabelText('Interface (YAML)') as HTMLTextAreaElement).value).toBe(PHI_SOURCE)
  })

  it('withdraws by taking data: phi out and saving — the same save, said plainly', async () => {
    getAgentInterface.mockResolvedValue(DECLARED)
    getAgentRepoHead.mockResolvedValue(headAt(SHA))
    saveAgentInterface.mockResolvedValue({ ...DECLARED, declarations: [] })
    render(<AgentInterfaceView agentSlug="labsdata" canEdit />)
    fireEvent.click(await screen.findByRole('button', { name: 'Withdraw PHI declaration' }))
    const box = screen.getByLabelText('Interface (YAML)') as HTMLTextAreaElement
    expect(declaresPhi(box.value)).toBe(false)
    expect(box.value).toContain('sites: [connect-labs]')
    expect(screen.getByTestId('phi-withdraw').textContent).toMatch(/withdraws the PHI-safe declaration of data/)
    expect(screen.queryByTestId('phi-declare')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Save and withdraw' }))
    await waitFor(() => expect(saveAgentInterface).toHaveBeenCalledWith('labsdata', box.value))
    await waitFor(() => expect(screen.queryByTestId('interface-declarations')).toBeNull())
  })
})
