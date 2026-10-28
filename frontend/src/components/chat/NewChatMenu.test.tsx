// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import type { AgentOut } from '@/api/agents'
import type { ChatSession } from '@/api/chat'
import type { RunnerOut } from '@/api/harness'

const createSession = vi.fn<(args: unknown) => Promise<ChatSession>>()
const getAgentPinnableRunners = vi.fn(() => Promise.resolve([]))
const getAgentRoutePreview = vi.fn(() => Promise.resolve({ summary: 'Goes to cloud-2, from ACE\'s runner order' }))

vi.mock('@/api/chat', () => ({ createSession }))
vi.mock('@/api/agents', () => ({ getAgentPinnableRunners, getAgentRoutePreview }))
vi.mock('@/api/harness', () => ({ listRunners: vi.fn(() => Promise.resolve([])) }))

const { NewChatMenu } = await import('./NewChatMenu')

const ace = { slug: 'ace', name: 'ACE', workspace: 'dimagi' } as AgentOut
const hal = { slug: 'hal', name: 'Hal', workspace: 'dimagi' } as AgentOut

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('NewChatMenu with a target (/ide per-project +)', () => {
  it('opens on the Run-on step for that agent and starts a session with it', async () => {
    createSession.mockResolvedValue({ id: 'new', workspace: 'dimagi' } as ChatSession)
    const onCreated = vi.fn()
    render(
      <MemoryRouter>
        <NewChatMenu
          agents={[ace, hal]}
          runners={[]}
          target={{ kind: 'agent', agent: ace }}
          label=""
          title="New session with ace"
          onError={vi.fn()}
          onCreated={onCreated}
        />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByLabelText('New session with ace'))
    expect(await screen.findByTestId('run-on-picker')).toBeTruthy()
    expect(screen.queryByText('Hal')).toBeNull()
    expect(screen.queryByText('Back')).toBeNull()
    fireEvent.click(screen.getByText('Start chat'))
    await waitFor(() => expect(onCreated).toHaveBeenCalled())
    expect(createSession).toHaveBeenCalledWith({ agentSlug: 'ace', workspace: 'dimagi', runnerId: undefined })
  })

  it('starts a repo session for a project target', async () => {
    createSession.mockResolvedValue({ id: 'new', workspace: 'dimagi' } as ChatSession)
    render(
      <MemoryRouter>
        <NewChatMenu
          agents={[]}
          runners={[]}
          target={{ kind: 'project', project: { slug: 'canopy-web', workspace: 'dimagi' } }}
          label=""
          title="New session with canopy-web"
          onError={vi.fn()}
          onCreated={vi.fn()}
        />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByLabelText('New session with canopy-web'))
    fireEvent.click(await screen.findByText('Start chat'))
    await waitFor(() =>
      expect(createSession).toHaveBeenCalledWith({ project: 'canopy-web', workspace: 'dimagi', runnerId: undefined }),
    )
  })
})

describe('NewChatMenu agent CLI choice', () => {
  function open(agent: AgentOut) {
    render(
      <MemoryRouter>
        <NewChatMenu agents={[agent]} runners={[]} target={{ kind: 'agent', agent }} label=""
                     title="New chat" onError={vi.fn()} onCreated={vi.fn()} />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByLabelText('New chat'))
  }

  it('offers no choice to an agent set up for one CLI', async () => {
    open({ ...ace, clis: ['claude-code'] } as AgentOut)
    expect(await screen.findByTestId('run-on-picker')).toBeTruthy()
    expect(screen.queryByTestId('new-chat-cli')).toBeNull()
  })

  it('starts the chat on the CLI picked, the default sending none', async () => {
    createSession.mockResolvedValue({ id: 'new', workspace: 'dimagi' } as ChatSession)
    open({ ...ace, clis: ['claude-code', 'codex'] } as AgentOut)
    const select = (await screen.findByTestId('new-chat-cli')) as HTMLSelectElement
    expect(Array.from(select.options).map((o) => o.text)).toEqual(['Claude Code (default)', 'Codex'])
    fireEvent.change(select, { target: { value: 'codex' } })
    fireEvent.click(screen.getByText('Start chat'))
    await waitFor(() => expect(createSession).toHaveBeenCalledWith(
      { agentSlug: 'ace', workspace: 'dimagi', runnerId: undefined, cli: 'codex' }))
  })
})

describe('NewChatMenu says what Auto does', () => {
  it('shows where the chat would go with Auto, and drops it once a runner is picked', async () => {
    getAgentPinnableRunners.mockResolvedValueOnce([
      { runner_id: 'r1', runner_name: 'jj-mbp', kind: 'emdash', online: true, ready: true, rank: null },
    ] as never)
    render(
      <MemoryRouter>
        <NewChatMenu agents={[ace]} runners={[]} target={{ kind: 'agent', agent: ace }} label=""
                     title="New chat" onError={vi.fn()} onCreated={vi.fn()} />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByLabelText('New chat'))
    expect((await screen.findByTestId('run-on-auto')).textContent).toBe('Auto: Goes to cloud-2, from ACE\'s runner order')
    const select = screen.getByTestId('run-on-select') as HTMLSelectElement
    await waitFor(() => expect(select.options.length).toBe(2))
    fireEvent.change(select, { target: { value: 'r1' } })
    expect(screen.queryByTestId('run-on-auto')).toBeNull()
  })
})

describe('NewChatMenu keeps the "Run on" list still while it is open', () => {
  const box = (id: string, name: string) => ({
    id, name, status: 'online', can_administer: true,
    capabilities: { sessions: true, projects: ['canopy-web'] },
  }) as unknown as RunnerOut

  it('a re-polled fleet in another order neither reorders the options nor moves the pick', async () => {
    createSession.mockResolvedValue({ id: 'new', workspace: 'dimagi' } as ChatSession)
    const target = { kind: 'project' as const, project: { slug: 'canopy-web', workspace: 'dimagi' } }
    const menu = (runners: RunnerOut[]) => (
      <MemoryRouter>
        <NewChatMenu agents={[]} runners={runners} target={target} label="" title="New session"
                     onError={vi.fn()} onCreated={vi.fn()} />
      </MemoryRouter>
    )
    // /api/runners/ comes back in heartbeat order, which changes poll to poll.
    const { rerender } = render(menu([box('z', 'cloud-zdr-1'), box('j', 'jj-mbp-cdp'), box('e', 'cloud-ec2-live')]))
    fireEvent.click(screen.getByLabelText('New session'))
    const select = (await screen.findByTestId('run-on-select')) as HTMLSelectElement
    const labels = () => Array.from(select.options).map((o) => o.textContent)
    expect(labels()).toEqual(['Auto', '● cloud-ec2-live', '● cloud-zdr-1', '● jj-mbp-cdp'])
    fireEvent.change(select, { target: { value: 'j' } })

    rerender(menu([box('j', 'jj-mbp-cdp'), box('e', 'cloud-ec2-live'), box('z', 'cloud-zdr-1')]))
    expect(labels()).toEqual(['Auto', '● cloud-ec2-live', '● cloud-zdr-1', '● jj-mbp-cdp'])
    expect(select.options[select.selectedIndex].textContent).toBe('● jj-mbp-cdp')
    fireEvent.click(screen.getByText('Start chat'))
    await waitFor(() => expect(createSession).toHaveBeenCalledWith(
      { project: 'canopy-web', workspace: 'dimagi', runnerId: 'j' }))
  })
})
