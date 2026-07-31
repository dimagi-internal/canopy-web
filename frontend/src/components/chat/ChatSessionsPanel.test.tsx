// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import type { AgentOut, AgentRunnerOut } from '@/api/agents'
import type { RunnerOut } from '@/api/harness'
import type { ChatSession, CloseResult } from '@/api/chat'
import type { ProjectSlug } from '@/api/projects'

// vi.mock is hoisted above these declarations, but the factories aren't
// invoked until the dynamic `import('./ChatSessionsPanel')` below — see
// RunnerAssignments.test.tsx for the same pattern.
const createSession = vi.fn<(input: unknown) => Promise<ChatSession>>()
const listSessions = vi.fn<() => Promise<ChatSession[]>>()
const closeSession = vi.fn<(id: string) => Promise<CloseResult>>()
const getAgentRunners = vi.fn<(slug: string) => Promise<AgentRunnerOut[]>>()
const listRunners = vi.fn<() => Promise<RunnerOut[]>>()
const listSlugs = vi.fn<() => Promise<ProjectSlug[]>>()

vi.mock('@/api/chat', () => ({ createSession, listSessions, closeSession }))
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
    paused: false,
    paused_note: '',
    paused_at: null,
    last_heartbeat_at: null,
    capabilities: { sessions: true },
    host: 'host',
    code_branch: 'main',
    code_version: '',
    code_sha: '',
    code_committed_at: 0,
    expected_code_committed_at: 0,
    expected_code_sha: '',
    workspace: null,
    paired_by_email: null,
    can_manage: true,
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

    // Flaky for a reason no timeout could fix — and the three earlier rounds
    // (#391 → 20s budget, #419 → 15s per-await, #434 → click inside act())
    // all missed the actual mechanism, so it kept failing on main.
    //
    // The trigger is `disabled={creating || (agents.length === 0 && projects.length === 0)}`.
    // This test renders with agents=[], so the button is disabled until
    // listSlugs() resolves and fills `projects`. findByText('New chat')
    // resolves the moment the TEXT exists — which is the first render, while
    // the button is still disabled. Clicking a disabled button is a no-op:
    // the menu never opens, and 'Acme' can never render.
    //
    // So it was a race between listSlugs() settling and the test clicking,
    // which is why it only lost on a loaded CI box, and why raising budgets
    // could never help: by the time anything started waiting, the single
    // click had already been swallowed and nothing would re-issue it.
    //
    // The siblings don't flake because they pass a non-empty `agents` prop,
    // which makes the trigger enabled on the very first render.
    //
    // Fix: wait on the CONDITION that makes the click meaningful (the button
    // being enabled) rather than on the text being present.
    const newChatButton = () => screen.getByText('New chat').closest('button')!
    await waitFor(() => expect(newChatButton().disabled).toBe(false))
    await act(async () => {
      fireEvent.click(newChatButton())
    })

    const acme = await screen.findByText('Acme')
    await act(async () => {
      fireEvent.click(acme)
    })

    const select = (await screen.findByTestId('run-on-select')) as HTMLSelectElement
    await waitFor(() => expect(listRunners).toHaveBeenCalled())
    await waitFor(() =>
      expect(Array.from(select.options).map((o) => o.textContent)).toEqual(['Auto', '● Alpha']),
    )
  })
})

function chatSession(id: string, overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    id,
    workspace: 'dimagi',
    agent_slug: 'echo',
    project: '',
    title: `Chat ${id}`,
    status: 'active',
    created_at: '2026-07-29T00:00:00Z',
    last_activity_at: '2026-07-29T00:00:00Z',
    origin: 'runner',
    running: false,
    runner_name: 'Laptop',
    runner_online: true,
    runner_status: 'online',
    session_key: '',
    ...overrides,
  } as ChatSession
}

// Sending to a paused/offline runner does not fail — it queues until that box
// comes back — so a chat on one must not sit in the list looking sendable.
describe('ChatSessionsPanel — sessions on a parked runner', () => {
  it('hides them by default and says how many, and why', async () => {
    listSlugs.mockResolvedValue([])
    listSessions.mockResolvedValue([
      chatSession('live', { title: 'Live one' }),
      chatSession('parked', { title: 'Parked one', runner_online: false, runner_status: 'paused' }),
    ])

    renderPanel([agent()])

    expect(await screen.findByText('Live one')).toBeTruthy()
    expect(screen.queryByText('Parked one')).toBeNull()
    expect(screen.getByTestId('parked-summary').textContent).toBe('1 hidden — runner paused')
  })

  it('reveals them dimmed, with the reason on the row, when Show offline is on', async () => {
    listSlugs.mockResolvedValue([])
    listSessions.mockResolvedValue([
      chatSession('live', { title: 'Live one' }),
      chatSession('parked', { title: 'Parked one', runner_online: false, runner_status: 'paused' }),
    ])

    renderPanel([agent()])
    fireEvent.click(await screen.findByTestId('toggle-offline'))

    expect(await screen.findByText('Parked one')).toBeTruthy()
    const row = screen.getByTestId('session-parked-parked')
    expect(row.className).toContain('opacity-60')
    expect(row.textContent).toContain('runner paused')
  })

  it('still offers the reveal toggle when EVERY session is parked', async () => {
    // The sort row used to render only for >1 session, so a lone parked chat
    // would hide with no control left on screen to bring it back.
    listSlugs.mockResolvedValue([])
    listSessions.mockResolvedValue([
      chatSession('only', { title: 'Only one', runner_online: false, runner_status: 'disconnected' }),
    ])

    renderPanel([agent()])

    expect(await screen.findByText('No chats on a live runner')).toBeTruthy()
    fireEvent.click(screen.getByTestId('toggle-offline'))
    expect(await screen.findByText('Only one')).toBeTruthy()
  })

  it('keeps an unbound web chat visible — it has no runner to be offline', async () => {
    listSlugs.mockResolvedValue([])
    listSessions.mockResolvedValue([
      chatSession('web', {
        title: 'Fresh chat',
        origin: 'web',
        runner_name: null,
        runner_online: null,
        runner_status: null,
      }),
    ])

    renderPanel([agent()])

    expect(await screen.findByText('Fresh chat')).toBeTruthy()
    expect(screen.queryByTestId('parked-summary')).toBeNull()
  })
})

describe('ChatSessionsPanel — close a session', () => {
  it('closes an idle session without asking', async () => {
    listSlugs.mockResolvedValue([])
    listSessions.mockResolvedValue([chatSession('s1', { running: false })])
    closeSession.mockResolvedValue({ ok: true, closing: false, reason: '' })

    renderPanel([agent()])

    const btn = await screen.findByTestId('close-session-s1')
    await act(async () => {
      fireEvent.click(btn)
    })
    expect(closeSession).toHaveBeenCalledWith('s1')
  })

  it('asks first when the agent is mid-turn', async () => {
    listSlugs.mockResolvedValue([])
    listSessions.mockResolvedValue([chatSession('s1', { running: true })])
    closeSession.mockResolvedValue({ ok: true, closing: true, reason: '' })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)

    renderPanel([agent()])

    const btn = await screen.findByTestId('close-session-s1')
    await act(async () => {
      fireEvent.click(btn)
    })
    expect(confirm).toHaveBeenCalled()
    expect(closeSession).not.toHaveBeenCalled()
  })

  it('does not navigate into the chat when Close is clicked', async () => {
    listSlugs.mockResolvedValue([])
    listSessions.mockResolvedValue([chatSession('s1')])
    closeSession.mockResolvedValue({ ok: true, closing: false, reason: '' })

    renderPanel([agent()])

    const btn = await screen.findByTestId('close-session-s1')
    expect(btn.closest('a')).toBeNull()
  })
})
