// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import type { AgentOut, AgentRunnerOut } from '@/api/agents'
import type { RunnerOut } from '@/api/harness'
import type { ChatSession } from '@/api/chat'
import type { ProjectSlug } from '@/api/projects'

// vi.mock is hoisted above these declarations, but the factories aren't
// invoked until the dynamic `import('./ChatSessionsPanel')` below — see
// RunnerAssignments.test.tsx for the same pattern.
const createSession = vi.fn<(input: unknown) => Promise<ChatSession>>()
const listSessions = vi.fn<() => Promise<ChatSession[]>>()
const getAgentRunners = vi.fn<(slug: string) => Promise<AgentRunnerOut[]>>()
const listRunners = vi.fn<() => Promise<RunnerOut[]>>()
const listSlugs = vi.fn<() => Promise<ProjectSlug[]>>()

vi.mock('@/api/chat', () => ({ createSession, listSessions }))
vi.mock('@/api/agents', () => ({ getAgentRunners, listAgents: vi.fn() }))
vi.mock('@/api/harness', () => ({ listRunners }))
vi.mock('@/api/projects', () => ({ projectsApi: { listSlugs } }))

const { ChatSessionsPanel } = await import('./ChatSessionsPanel')

function agent(overrides: Partial<AgentOut> = {}): AgentOut {
  return {
    slug: 'echo',
    name: 'Echo',
    workspace: 'dimagi',
    ...overrides,
  } as AgentOut
}

function agentRunner(id: string, overrides: Partial<AgentRunnerOut> = {}): AgentRunnerOut {
  return {
    runner_id: id,
    runner_name: `Runner ${id}`,
    kind: 'emdash',
    rank: 1,
    online: true,
    ready: true,
    enabled: true,
    ...overrides,
  }
}

function fleetRunner(id: string, overrides: Partial<RunnerOut> = {}): RunnerOut {
  return {
    id,
    name: `Runner ${id}`,
    kind: 'emdash',
    status: 'online',
    status_note: '',
    ready: true,
    ready_note: '',
    last_heartbeat_at: null,
    capabilities: { sessions: true },
    host: 'host',
    code_branch: 'main',
    workspace: null,
    paired_by_email: null,
    ...overrides,
  }
}

function renderPanel(agents: AgentOut[]) {
  return render(
    <MemoryRouter>
      <ChatSessionsPanel agents={agents} />
    </MemoryRouter>,
  )
}

// A promise plus its resolve/reject, so a test can control exactly when a
// getAgentRunners call settles — needed to reproduce the out-of-order-resolve
// race. Mirrors RunnerAssignments.test.tsx's deferred idiom.
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ChatSessionsPanel — Run on picker', () => {
  it('lists the agent’s assigned runners with an online/offline marker, defaulting to Auto', async () => {
    listSessions.mockResolvedValue([])
    listSlugs.mockResolvedValue([])
    getAgentRunners.mockResolvedValue([
      agentRunner('r1', { runner_name: 'Laptop', online: true }),
      agentRunner('r2', { runner_name: 'Cloud', online: false }),
    ])

    renderPanel([agent()])

    fireEvent.click(await screen.findByText('New chat'))
    fireEvent.click(await screen.findByText('Echo'))

    const select = (await screen.findByTestId('run-on-select')) as HTMLSelectElement
    await waitFor(() => expect(getAgentRunners).toHaveBeenCalledWith('echo'))

    const options = Array.from(select.options).map((o) => o.textContent)
    expect(options).toEqual(['Auto', '● Laptop', '○ Cloud'])
    expect(select.value).toBe('')
  })

  it('passes the chosen runnerId to createSession when a specific runner is picked', async () => {
    listSessions.mockResolvedValue([])
    listSlugs.mockResolvedValue([])
    getAgentRunners.mockResolvedValue([agentRunner('r1', { runner_name: 'Laptop', online: true })])
    createSession.mockResolvedValue({
      id: 's1',
      workspace: 'dimagi',
      agent_slug: 'echo',
      project: '',
      title: '',
      status: 'active',
      created_at: '2026-07-23T00:00:00Z',
      origin: 'web',
      running: false,
      session_key: '',
    } as ChatSession)

    renderPanel([agent()])

    fireEvent.click(await screen.findByText('New chat'))
    fireEvent.click(await screen.findByText('Echo'))
    const select = (await screen.findByTestId('run-on-select')) as HTMLSelectElement
    await waitFor(() => expect(getAgentRunners).toHaveBeenCalled())

    fireEvent.change(select, { target: { value: 'r1' } })
    await act(async () => {
      fireEvent.click(screen.getByText('Start chat'))
    })

    await waitFor(() =>
      expect(createSession).toHaveBeenCalledWith(
        expect.objectContaining({ agentSlug: 'echo', runnerId: 'r1' }),
      ),
    )
  })

  it('omits runnerId when Auto is left selected', async () => {
    listSessions.mockResolvedValue([])
    listSlugs.mockResolvedValue([])
    getAgentRunners.mockResolvedValue([agentRunner('r1', { runner_name: 'Laptop', online: true })])
    createSession.mockResolvedValue({
      id: 's2',
      workspace: 'dimagi',
      agent_slug: 'echo',
      project: '',
      title: '',
      status: 'active',
      created_at: '2026-07-23T00:00:00Z',
      origin: 'web',
      running: false,
      session_key: '',
    } as ChatSession)

    renderPanel([agent()])

    fireEvent.click(await screen.findByText('New chat'))
    fireEvent.click(await screen.findByText('Echo'))
    await screen.findByTestId('run-on-select')

    await act(async () => {
      fireEvent.click(screen.getByText('Start chat'))
    })

    await waitFor(() => expect(createSession).toHaveBeenCalled())
    const call = createSession.mock.calls[0][0] as { runnerId?: string }
    expect(call.runnerId).toBeUndefined()
  })

  it('does not let a stale getAgentRunners response overwrite a later pick', async () => {
    listSessions.mockResolvedValue([])
    listSlugs.mockResolvedValue([])

    const first = deferred<AgentRunnerOut[]>()
    const second = deferred<AgentRunnerOut[]>()
    getAgentRunners
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)

    renderPanel([
      agent({ slug: 'echo', name: 'Echo' }),
      agent({ slug: 'hal', name: 'Hal', workspace: 'dimagi' }),
    ])

    // Pick Echo — still in flight.
    fireEvent.click(await screen.findByText('New chat'))
    fireEvent.click(await screen.findByText('Echo'))
    await waitFor(() => expect(getAgentRunners).toHaveBeenNthCalledWith(1, 'echo'))

    // Before it resolves, go back and pick Hal instead — also in flight.
    fireEvent.click(screen.getByText('Back'))
    fireEvent.click(await screen.findByText('Hal'))
    await waitFor(() => expect(getAgentRunners).toHaveBeenNthCalledWith(2, 'hal'))

    // Resolve the NEWER (Hal) request first.
    await act(async () => {
      second.resolve([agentRunner('r2', { runner_name: 'Cloud', online: true })])
    })
    const select = (await screen.findByTestId('run-on-select')) as HTMLSelectElement
    await waitFor(() =>
      expect(Array.from(select.options).map((o) => o.textContent)).toEqual(['Auto', '● Cloud']),
    )

    // The stale OLDER (Echo) response landing late must not overwrite Hal's options.
    await act(async () => {
      first.resolve([agentRunner('r1', { runner_name: 'Laptop', online: true })])
    })
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual(['Auto', '● Cloud'])
  })

  it('filters project "Run on" options to online + sessions-capable fleet runners', async () => {
    listSessions.mockResolvedValue([])
    listSlugs.mockResolvedValue([{ slug: 'acme', name: 'Acme', workspace: 'dimagi' } as ProjectSlug])
    listRunners.mockResolvedValue([
      fleetRunner('a', { name: 'Alpha', status: 'online', capabilities: { sessions: true } }),
      fleetRunner('b', { name: 'Beta', status: 'offline', capabilities: { sessions: true } }),
      fleetRunner('c', { name: 'Gamma', status: 'online', capabilities: {} }),
    ])

    renderPanel([])

    fireEvent.click(await screen.findByText('New chat'))
    fireEvent.click(await screen.findByText('Acme'))

    const select = (await screen.findByTestId('run-on-select')) as HTMLSelectElement
    await waitFor(() => expect(listRunners).toHaveBeenCalled())
    await waitFor(() =>
      expect(Array.from(select.options).map((o) => o.textContent)).toEqual(['Auto', '● Alpha']),
    )
  })
})
