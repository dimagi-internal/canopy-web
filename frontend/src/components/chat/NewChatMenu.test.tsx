// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import type { AgentOut } from '@/api/agents'
import type { ChatSession } from '@/api/chat'

const createSession = vi.fn<(args: unknown) => Promise<ChatSession>>()
const getAgentDefaultOrder = vi.fn(() => Promise.resolve({ own: false, runners: [] }))

vi.mock('@/api/chat', () => ({ createSession }))
vi.mock('@/api/agents', () => ({ getAgentDefaultOrder, getAgentRunners: vi.fn() }))
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
    expect(createSession).toHaveBeenCalledWith({ agentSlug: 'ace', workspace: 'dimagi', runnerId: undefined,
                                                 unlock: expect.any(Function) })
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
      expect(createSession).toHaveBeenCalledWith({ project: 'canopy-web', workspace: 'dimagi', runnerId: undefined,
                                                   unlock: expect.any(Function) }),
    )
  })
})
