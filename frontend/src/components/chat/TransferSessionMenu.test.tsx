// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { RunnerOut } from '@/api/harness'
import type { ChatSession, TransferResult } from '@/api/chat'

const transferSession = vi.fn<(id: string, runner: string, brief?: string) => Promise<TransferResult>>()
const listRunners = vi.fn<() => Promise<RunnerOut[]>>()

vi.mock('@/api/chat', () => ({ transferSession }))
vi.mock('@/api/harness', () => ({ listRunners }))

const { TransferSessionMenu } = await import('./TransferSessionMenu')

function runner(id: string, overrides: Partial<RunnerOut> = {}): RunnerOut {
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
    owner_email: null,
    can_manage: true,
    can_administer: true,
    flags: [],
    known_flags: ['zdr'],
    ...overrides,
  } as RunnerOut
}

function session(overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    id: 's1',
    workspace: 'dimagi',
    agent_slug: 'echo',
    project: '',
    title: 'Chat s1',
    status: 'active',
    created_at: '2026-10-05T00:00:00Z',
    last_activity_at: '2026-10-05T00:00:00Z',
    origin: 'runner',
    running: false,
    runner_name: 'Laptop',
    runner_online: true,
    runner_status: 'online',
    session_key: '',
    ...overrides,
  } as ChatSession
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('TransferSessionMenu', () => {
  it('disables the trigger for a session with no runner binding', () => {
    render(<TransferSessionMenu session={session({ runner_name: null })} onResult={vi.fn()} />)
    expect((screen.getByTestId('transfer-session-s1') as HTMLButtonElement).disabled).toBe(true)
  })

  it('disables the trigger for an archived session', () => {
    render(<TransferSessionMenu session={session({ status: 'archived' })} onResult={vi.fn()} />)
    expect((screen.getByTestId('transfer-session-s1') as HTMLButtonElement).disabled).toBe(true)
  })

  it('loads the fleet lazily, excluding the session’s current runner and anything offline', async () => {
    listRunners.mockResolvedValue([
      runner('a', { name: 'Laptop' }), // current runner — excluded
      runner('b', { name: 'Cloud' }),
      runner('c', { name: 'Offline box', status: 'offline' }),
      runner('d', { name: 'No sessions', capabilities: {} }),
    ])

    render(<TransferSessionMenu session={session()} onResult={vi.fn()} />)
    expect(listRunners).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('transfer-session-s1'))
    const select = (await screen.findByTestId('transfer-runner-select')) as HTMLSelectElement
    await waitFor(() =>
      expect(Array.from(select.options).map((o) => o.textContent)).toEqual(['Pick a runner', 'Cloud']),
    )
  })

  it('transfers to the picked runner with the handoff note, and reports the result', async () => {
    listRunners.mockResolvedValue([runner('b', { name: 'Cloud' })])
    transferSession.mockResolvedValue({
      session_id: 's1', runner: 'Cloud', transferred_from: 'Laptop', index_offset: 3,
      turn_id: 't1', status: 'moved', request_id: null, approvers: [],
    })
    const onResult = vi.fn()

    render(<TransferSessionMenu session={session()} onResult={onResult} />)
    fireEvent.click(screen.getByTestId('transfer-session-s1'))
    const select = (await screen.findByTestId('transfer-runner-select')) as HTMLSelectElement
    fireEvent.change(select, { target: { value: 'b' } })
    fireEvent.change(screen.getByTestId('transfer-brief'), { target: { value: 'branch pushed, PR open' } })

    await act(async () => {
      fireEvent.click(screen.getByText('Transfer'))
    })

    expect(transferSession).toHaveBeenCalledWith('s1', 'b', 'branch pushed, PR open')
    await waitFor(() => expect(onResult).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'moved', runner: 'Cloud' }), null,
    ))
  })

  it('reports a pending request without pretending anything moved', async () => {
    listRunners.mockResolvedValue([runner('b', { name: 'Cloud' })])
    transferSession.mockResolvedValue({
      session_id: 's1', runner: '', transferred_from: '', index_offset: 0,
      turn_id: '', status: 'pending', request_id: 'req1', approvers: ['owner@example.com'],
    })
    const onResult = vi.fn()

    render(<TransferSessionMenu session={session()} onResult={onResult} />)
    fireEvent.click(screen.getByTestId('transfer-session-s1'))
    const select = (await screen.findByTestId('transfer-runner-select')) as HTMLSelectElement
    fireEvent.change(select, { target: { value: 'b' } })

    await act(async () => {
      fireEvent.click(screen.getByText('Transfer'))
    })

    await waitFor(() => expect(onResult).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'pending', approvers: ['owner@example.com'] }), null,
    ))
  })

  it('reports the error message on refusal, leaving the session untouched', async () => {
    listRunners.mockResolvedValue([runner('b', { name: 'Cloud' })])
    transferSession.mockRejectedValue(new Error('a turn is executing — stop the session first'))
    const onResult = vi.fn()

    render(<TransferSessionMenu session={session()} onResult={onResult} />)
    fireEvent.click(screen.getByTestId('transfer-session-s1'))
    const select = (await screen.findByTestId('transfer-runner-select')) as HTMLSelectElement
    fireEvent.change(select, { target: { value: 'b' } })

    await act(async () => {
      fireEvent.click(screen.getByText('Transfer'))
    })

    await waitFor(() => expect(onResult).toHaveBeenCalledWith(
      null, 'a turn is executing — stop the session first',
    ))
  })
})
