// @vitest-environment jsdom
import type { ReactNode } from 'react'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SessionMenu } from 'canopy-ui/chat'

import type { ChatSessionDetail } from '@/api/chat'
import type { RunnerOut } from '@/api/harness'
import { useAttachments } from './useAttachments'
import { useMenuAnswer } from './useMenuAnswer'
import { useMessageFork } from './useMessageFork'
import { useRunnerPlacement } from './useRunnerPlacement'
import { useSessionActions } from './useSessionActions'
import { useSessionMeta } from './useSessionMeta'
import { useTranscriptPaging } from './useTranscriptPaging'

const chat = vi.hoisted(() => ({
  answerMenu: vi.fn(),
  closeSession: vi.fn(),
  deleteAttachment: vi.fn(),
  detachSessionFromSlack: vi.fn(),
  forkSession: vi.fn(),
  getSession: vi.fn(),
  listMessages: vi.fn(),
  moveSession: vi.fn(),
  placeTurn: vi.fn(),
  requestBackfill: vi.fn(),
  resetSession: vi.fn(),
  setSessionNotify: vi.fn(),
  setSessionTitle: vi.fn(),
  uploadAttachment: vi.fn(),
}))
const harness = vi.hoisted(() => ({ listRunners: vi.fn(), unpauseRunner: vi.fn() }))
const agents = vi.hoisted(() => ({ getAgent: vi.fn() }))
const sessionWorkspace = vi.hoisted(() => ({ getSessionWorkspacePreference: vi.fn() }))
const nav = vi.hoisted(() => ({ navigate: vi.fn() }))

vi.mock('@/api/chat', async (orig) => ({ ...(await orig<object>()), ...chat }))
vi.mock('@/api/harness', async (orig) => ({ ...(await orig<object>()), ...harness }))
vi.mock('@/api/agents', async (orig) => ({ ...(await orig<object>()), ...agents }))
vi.mock('@/api/sessionWorkspace', () => sessionWorkspace)
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<object>()),
  useNavigate: () => nav.navigate,
}))

const wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter>

const meta = (over: Partial<ChatSessionDetail> = {}): ChatSessionDetail =>
  ({
    id: 's1',
    title: 'T',
    workspace: 'ws',
    runner_name: 'box',
    runner_online: null,
    runner_status: null,
    my_role: 'owner',
    can_move: false,
    notify_every_completion: false,
    has_more_before: true,
    oldest_loaded_turn_index: 10,
    messages: [],
    status: 'active',
    ...over,
  }) as unknown as ChatSessionDetail

const runner = (over: Partial<RunnerOut> = {}): RunnerOut =>
  ({ id: 'r1', name: 'box', status: 'online', paused: false, can_manage: true, ...over }) as unknown as RunnerOut

const menu: SessionMenu = { title: 'Allow?', options: [{ number: 1, label: 'Yes' }] } as SessionMenu

beforeEach(() => {
  vi.clearAllMocks()
  harness.listRunners.mockResolvedValue([])
})
afterEach(cleanup)

describe('useAttachments', () => {
  it('shows an uploading chip, swaps in the server id, and clears', async () => {
    chat.uploadAttachment.mockResolvedValue({ id: 'a1', filename: 'x.png' })
    const { result } = renderHook(() => useAttachments('s1'))
    act(() => result.current.attach([new File(['x'], 'x.png')]))
    expect(result.current.attachments[0]).toMatchObject({ filename: 'x.png', uploading: true })
    await waitFor(() => expect(result.current.attachments).toEqual([{ id: 'a1', filename: 'x.png' }]))
    act(() => result.current.clear())
    expect(result.current.attachments).toEqual([])
  })

  it('marks a failed upload and forgets it without a server call', async () => {
    chat.uploadAttachment.mockRejectedValue(new Error('too big'))
    const { result } = renderHook(() => useAttachments('s1'))
    act(() => result.current.attach([new File(['x'], 'x.png')]))
    await waitFor(() => expect(result.current.attachments[0]).toMatchObject({ error: 'too big', uploading: false }))
    act(() => result.current.remove(result.current.attachments[0].id))
    expect(result.current.attachments).toEqual([])
    expect(chat.deleteAttachment).not.toHaveBeenCalled()
  })

  it('removing a saved attachment deletes it server-side', () => {
    chat.deleteAttachment.mockResolvedValue(undefined)
    const { result } = renderHook(() => useAttachments('s1'))
    act(() => result.current.remove('a1'))
    expect(chat.deleteAttachment).toHaveBeenCalledWith('a1')
  })
})

describe('useMenuAnswer', () => {
  it('hides the answered menu at tap time', async () => {
    chat.answerMenu.mockResolvedValue({ ok: true })
    const { result } = renderHook(() => useMenuAnswer('s1', menu, 'blocked'))
    expect(result.current.visibleMenu).toBe(menu)
    await act(async () => { await result.current.answer(1) })
    expect(chat.answerMenu).toHaveBeenCalledWith('s1', 1, undefined, undefined)
    expect(result.current.menuHidden).toBe(true)
    expect(result.current.visibleMenu).toBeUndefined()
  })

  it('puts the menu back with the reason when the answer is not relayed', async () => {
    chat.answerMenu.mockResolvedValue({ ok: false, reason: 'unavailable' })
    const { result } = renderHook(() => useMenuAnswer('s1', menu, 'blocked'))
    await act(async () => { await result.current.answer(1) })
    expect(result.current.visibleMenu).toBe(menu)
    expect(result.current.answerError).toMatch(/offline/)
  })

  it('lets go of the answer once the agent is working again', async () => {
    chat.answerMenu.mockResolvedValue({ ok: true })
    const { result, rerender } = renderHook(({ m, a }) => useMenuAnswer('s1', m, a), {
      initialProps: { m: menu as SessionMenu | undefined, a: 'blocked' as 'blocked' | 'working' },
    })
    await act(async () => { await result.current.answer(1) })
    expect(result.current.pendingAnswer).not.toBeNull()
    rerender({ m: undefined, a: 'working' })
    expect(result.current.pendingAnswer).toBeNull()
  })
})

describe('useSessionMeta', () => {
  it('loads the detail, hands it to onLoaded, and resolves the agent name', async () => {
    chat.getSession.mockResolvedValue(meta({ agent_slug: 'ada' } as Partial<ChatSessionDetail>))
    agents.getAgent.mockResolvedValue({ name: 'Ada' })
    const onLoaded = vi.fn()
    const { result } = renderHook(() => useSessionMeta('s1', onLoaded))
    await waitFor(() => expect(result.current.agentName).toBe('Ada'))
    expect(result.current.meta?.id).toBe('s1')
    expect(onLoaded).toHaveBeenCalledTimes(1)
  })

  it('falls back to the slug and reports a missing session', async () => {
    chat.getSession.mockResolvedValueOnce(meta({ agent_slug: 'ada' } as Partial<ChatSessionDetail>))
    agents.getAgent.mockRejectedValue(new Error('nope'))
    const { result, rerender } = renderHook(({ id }) => useSessionMeta(id), { initialProps: { id: 's1' } })
    await waitFor(() => expect(result.current.agentName).toBe('ada'))
    chat.getSession.mockRejectedValueOnce(new Error('session not found'))
    rerender({ id: 's2' })
    await waitFor(() => expect(result.current.metaError).toBe('session not found'))
    expect(result.current.meta).toBeNull()
  })
})

describe('useTranscriptPaging', () => {
  it('loads an earlier page from the seeded cursor', async () => {
    chat.listMessages.mockResolvedValue({
      messages: [{ turn_index: 5, role: 'user', content: {} }],
      has_more_before: false,
    })
    const prepend = vi.fn()
    const { result } = renderHook(() => useTranscriptPaging('s1', prepend))
    act(() => result.current.seed(meta()))
    expect(result.current.hasMoreBefore).toBe(true)
    await act(async () => { await result.current.loadEarlier() })
    expect(chat.listMessages).toHaveBeenCalledWith('s1', 10)
    expect(prepend).toHaveBeenCalledTimes(1)
    expect(result.current.hasMoreBefore).toBe(false)
  })

  it('reports full history unavailable when the runner is offline', async () => {
    chat.requestBackfill.mockResolvedValue({ status: 'unavailable' })
    const { result } = renderHook(() => useTranscriptPaging('s1', vi.fn()))
    await act(async () => { await result.current.loadFull() })
    expect(result.current.historyUnavailable).toBe(true)
    expect(result.current.loadingFull).toBe(false)
  })

  it('reads the full transcript once after the backfill', async () => {
    chat.requestBackfill.mockResolvedValue({ status: 'complete' })
    chat.getSession.mockResolvedValue(meta({ messages: [], oldest_loaded_turn_index: 0 }))
    const prepend = vi.fn()
    const { result } = renderHook(() => useTranscriptPaging('s1', prepend))
    await act(async () => { await result.current.loadFull() })
    expect(chat.getSession).toHaveBeenCalledWith('s1', { full: true })
    expect(prepend).toHaveBeenCalledTimes(1)
    expect(result.current.hasMoreBefore).toBe(false)
  })
})

describe('useRunnerPlacement', () => {
  it('derives offline from the fleet and offers the online runners', async () => {
    harness.listRunners.mockResolvedValue([
      runner({ status: 'offline' }),
      runner({ id: 'r2', name: 'other', status: 'online', capabilities: { sessions: true } } as Partial<RunnerOut>),
    ])
    const setMeta = vi.fn()
    const { result } = renderHook(() => useRunnerPlacement('s1', meta(), setMeta))
    await waitFor(() => expect(result.current.boundOffline).toBe(true))
    expect(result.current.boundRunner?.id).toBe('r1')
    expect(result.current.boundPaused).toBe(false)
  })

  it('places the turn on another runner and says so', async () => {
    chat.placeTurn.mockResolvedValue({})
    const { result } = renderHook(() => useRunnerPlacement('s1', meta(), vi.fn()))
    await act(async () => {
      result.current.continueOn('r2')
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.placeInfo).toMatch(/Placed/))
    expect(chat.placeTurn).toHaveBeenCalledWith('s1', 'r2')
    expect(result.current.placing).toBe(false)
  })

  it('shows the server refusal on a 422', async () => {
    const { ChatApiError } = await import('@/api/chat')
    chat.placeTurn.mockRejectedValue(new ChatApiError(422, 'not allowed here'))
    const { result } = renderHook(() => useRunnerPlacement('s1', meta(), vi.fn()))
    act(() => result.current.waitForIt())
    await waitFor(() => expect(result.current.placeError).toBe('not allowed here'))
  })

  it('resuming a paused runner patches the fleet and drops the stale liveness', async () => {
    harness.listRunners.mockResolvedValue([runner({ status: 'offline', paused: true })])
    harness.unpauseRunner.mockResolvedValue(runner({ status: 'online', paused: false }))
    const setMeta = vi.fn()
    const { result } = renderHook(() => useRunnerPlacement('s1', meta(), setMeta))
    await waitFor(() => expect(result.current.boundPaused).toBe(true))
    act(() => result.current.resumeBoundRunner())
    await waitFor(() => expect(result.current.placeInfo).toBe('box resumed.'))
    expect(result.current.boundOffline).toBe(false)
    expect(setMeta).toHaveBeenCalled()
  })
})

describe('useSessionActions', () => {
  it('renames and reports the new title', async () => {
    chat.setSessionTitle.mockResolvedValue({ title: 'New' })
    const setMeta = vi.fn()
    const onTitleChange = vi.fn()
    const { result } = renderHook(
      () => useSessionActions({ sessionId: 's1', meta: meta(), setMeta, onTitleChange, editTitle: true }),
      { wrapper },
    )
    expect(result.current.renaming).toBe(true)
    await act(async () => { await result.current.renameTo('New') })
    expect(result.current.renaming).toBe(false)
    expect(onTitleChange).toHaveBeenCalled()
    expect(setMeta).toHaveBeenCalled()
  })

  it('closes and leaves when the server closed it outright', async () => {
    chat.closeSession.mockResolvedValue({ ok: true, closing: false })
    const onLeave = vi.fn()
    const { result } = renderHook(
      () => useSessionActions({ sessionId: 's1', meta: meta(), setMeta: vi.fn(), onLeave }),
      { wrapper },
    )
    await act(async () => { await result.current.closeThisSession() })
    expect(chat.closeSession).toHaveBeenCalledWith('s1')
    expect(onLeave).toHaveBeenCalled()
  })

  it('describes a reset the runner could not do', async () => {
    chat.resetSession.mockResolvedValue({ ok: false, reason: 'runner_unreachable' })
    const { result } = renderHook(
      () => useSessionActions({ sessionId: 's1', meta: meta(), setMeta: vi.fn() }),
      { wrapper },
    )
    await act(async () => { await result.current.resetFromTranscript() })
    expect(result.current.resetNote).toMatch(/offline/)
  })

  it('rolls back the notify toggle when the server refuses', async () => {
    chat.setSessionNotify.mockRejectedValue(new Error('x'))
    const updates: Array<boolean | undefined> = []
    let current: ChatSessionDetail | null = meta()
    const setMeta = (fn: unknown) => {
      current = (fn as (p: ChatSessionDetail | null) => ChatSessionDetail | null)(current)
      updates.push(current?.notify_every_completion)
    }
    const { result } = renderHook(
      () => useSessionActions({ sessionId: 's1', meta: meta(), setMeta: setMeta as never }),
      { wrapper },
    )
    await act(async () => { await result.current.toggleNotify() })
    expect(updates).toEqual([true, false])
  })

  it('only a manager may manage; move targets load only when movable', async () => {
    sessionWorkspace.getSessionWorkspacePreference.mockResolvedValue({ choices: [{ slug: 'b' }] })
    const { result } = renderHook(
      () => useSessionActions({ sessionId: 's1', meta: meta({ my_role: 'viewer', can_move: true } as Partial<ChatSessionDetail>), setMeta: vi.fn() }),
      { wrapper },
    )
    expect(result.current.canManage).toBe(false)
    await waitFor(() => expect(result.current.moveTargets).toEqual([{ slug: 'b' }]))
  })
})

describe('useMessageFork', () => {
  it('opens on a forkable row and navigates to the fork', async () => {
    chat.forkSession.mockResolvedValue({ id: 'f1', workspace: 'ws' })
    const fleet = [runner({ kind: 'cloud' } as Partial<RunnerOut>)]
    const messages = [{ turn_index: 3, status: 'done' }] as never
    const { result } = renderHook(() => useMessageFork('s1', 'box', fleet, messages))
    const target = document.createElement('div')
    target.setAttribute('data-turn-index', '3')
    const preventDefault = vi.fn()
    act(() => result.current.onMessageContextMenu({ target, clientX: 1, clientY: 2, preventDefault } as never))
    expect(result.current.messageMenu).toEqual({ x: 1, y: 2, turnIndex: 3 })
    expect(preventDefault).toHaveBeenCalled()
    await act(async () => { await result.current.forkHere() })
    expect(chat.forkSession).toHaveBeenCalledWith('s1', 3)
    expect(nav.navigate).toHaveBeenCalled()
  })

  it('leaves the browser menu alone when nothing can fork', () => {
    const preventDefault = vi.fn()
    const { result } = renderHook(() => useMessageFork('s1', null, [], []))
    act(() => result.current.onMessageContextMenu({ target: document.body, preventDefault } as never))
    expect(preventDefault).not.toHaveBeenCalled()
    expect(result.current.messageMenu).toBeNull()
  })
})
