// @vitest-environment jsdom
import { act, cleanup, render, renderHook, screen, fireEvent, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { PersonFactDetail } from '@/api/people'
import { FactRow, sourceOf } from './FactRow'
import { usePersonFacts } from './usePersonFacts'

const people = vi.hoisted(() => ({
  addMyEntry: vi.fn(),
  correctMyEntry: vi.fn(),
  getMyPerson: vi.fn(),
  getPersonPage: vi.fn(),
  listMyAudit: vi.fn(),
  listMyCollections: vi.fn(),
  listMyGrants: vi.fn(),
  listPersonPageAudit: vi.fn(),
  retractFact: vi.fn(),
  revokeMyGrant: vi.fn(),
  setMyAgentMemory: vi.fn(),
}))
const workspaces = vi.hoisted(() => ({ listWorkspaces: vi.fn() }))
vi.mock('@/api/people', async (orig) => ({ ...(await orig<object>()), ...people }))
vi.mock('@/api/workspaces', async (orig) => ({ ...(await orig<object>()), ...workspaces }))

const me = {
  id: 7,
  display_name: 'Jo Bloggs',
  agent_memory: { record: { available: true, default: false }, use: { available: true, default: true } },
  facts: [],
}
const grant = (over: object = {}) => ({
  grantId: 'g1',
  status: 'active',
  grantType: 'persistent',
  issuedAt: '2026-10-01T00:00:00Z',
  client: { id: 'c1', name: 'ACE over Slack' },
  canopy: { agent: 'ace', channel: 'slack:T1' },
  ...over,
})
const fact = (over: Partial<PersonFactDetail> = {}) =>
  ({ id: 1, entry_id: 'e1', statement: 'Likes tea', workspace: null, basis: 'declared', ...over }) as PersonFactDetail

beforeEach(() => {
  vi.clearAllMocks()
  people.getMyPerson.mockResolvedValue(me)
  people.listMyGrants.mockResolvedValue([grant(), grant({ grantId: 'g0', status: 'revoked' })])
  people.listMyAudit.mockResolvedValue({ events: [{ eventId: 'a1', eventType: 'preference.read' }, { eventId: 'a2', eventType: 'preference.created' }], nextCursor: 'c1' })
  people.listMyCollections.mockResolvedValue({ collections: [] })
  workspaces.listWorkspaces.mockResolvedValue([{ slug: 'ws' }])
  vi.stubGlobal('confirm', vi.fn(() => true))
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('usePersonFacts', () => {
  it('loads your page: record, grants, audit, collections, workspaces', async () => {
    const { result } = renderHook(() => usePersonFacts())
    await waitFor(() => expect(result.current.me?.id).toBe(7))
    await waitFor(() => expect(result.current.workspaces).toHaveLength(1))
    expect(result.current.past).toHaveLength(1)
    expect(result.current.names.get('ace')).toBe('ACE')
    // Reads live in the per-conversation history; the change log leaves them out.
    expect(result.current.changes.map((e) => e.eventId)).toEqual(['a2'])
    expect(result.current.nextCursor).toBe('c1')
  })

  it('reads someone else’s page through the viewer routes only', async () => {
    people.getPersonPage.mockResolvedValue({ me, grants: [], collections: null })
    people.listPersonPageAudit.mockResolvedValue({ events: [], nextCursor: null })
    const { result } = renderHook(() => usePersonFacts(42))
    await waitFor(() => expect(result.current.me).not.toBeNull())
    expect(people.getPersonPage).toHaveBeenCalledWith(42)
    expect(people.getMyPerson).not.toHaveBeenCalled()
    expect(workspaces.listWorkspaces).not.toHaveBeenCalled()
  })

  it('a record that does not load is fatal; a section that does not is a banner', async () => {
    people.getMyPerson.mockRejectedValue(new Error('gone'))
    people.listMyGrants.mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => usePersonFacts())
    await waitFor(() => expect(result.current.fatal).toBe('gone'))
    await waitFor(() => expect(result.current.error).toMatch(/which agents can read your memory: nope/))
  })

  it('forgets an entry after confirming, then re-reads', async () => {
    people.retractFact.mockResolvedValue(undefined)
    const { result } = renderHook(() => usePersonFacts())
    await waitFor(() => expect(result.current.me).not.toBeNull())
    const before = people.getMyPerson.mock.calls.length
    act(() => result.current.retract(fact()))
    await waitFor(() => expect(people.retractFact).toHaveBeenCalledWith(7, 1))
    await waitFor(() => expect(people.getMyPerson.mock.calls.length).toBe(before + 1))
    expect(result.current.busy).toBeNull()
  })

  it('does nothing when the confirm is declined', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false))
    const { result } = renderHook(() => usePersonFacts())
    await waitFor(() => expect(result.current.me).not.toBeNull())
    act(() => result.current.retract(fact()))
    act(() => result.current.revoke({ key: 'ace', name: 'ACE', grants: [grant()] } as never))
    expect(people.retractFact).not.toHaveBeenCalled()
    expect(people.revokeMyGrant).not.toHaveBeenCalled()
  })

  it('corrects only a changed, non-blank text', async () => {
    people.correctMyEntry.mockResolvedValue(undefined)
    const { result } = renderHook(() => usePersonFacts())
    await waitFor(() => expect(result.current.me).not.toBeNull())
    act(() => result.current.correct(fact(), '  Likes tea  '))
    act(() => result.current.correct(fact(), '   '))
    expect(people.correctMyEntry).not.toHaveBeenCalled()
    act(() => result.current.correct(fact(), 'Likes coffee'))
    await waitFor(() => expect(people.correctMyEntry).toHaveBeenCalledWith('e1', 'Likes coffee'))
  })

  it('revokes every grant behind an agent’s access', async () => {
    people.revokeMyGrant.mockResolvedValue(undefined)
    const { result } = renderHook(() => usePersonFacts())
    await waitFor(() => expect(result.current.me).not.toBeNull())
    act(() => result.current.revoke({ key: 'ace', name: 'ACE', grants: [grant(), grant({ grantId: 'g2' })] } as never))
    await waitFor(() => expect(people.revokeMyGrant).toHaveBeenCalledTimes(2))
  })

  it('turning a switch off confirms; turning one on does not', async () => {
    people.setMyAgentMemory.mockResolvedValue(undefined)
    const confirmFn = vi.fn(() => true)
    vi.stubGlobal('confirm', confirmFn)
    const { result } = renderHook(() => usePersonFacts())
    await waitFor(() => expect(result.current.me).not.toBeNull())
    act(() => result.current.toggleAvailable('record'))
    expect(confirmFn).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(people.setMyAgentMemory).toHaveBeenCalledWith({ record: { available: false } }))
    act(() => result.current.toggleDefault('use'))
    await waitFor(() => expect(people.setMyAgentMemory).toHaveBeenCalledWith({ use: { default: false } }))
    expect(confirmFn).toHaveBeenCalledTimes(1)
  })

  it('add resolves whether the entry landed, and reports a failure', async () => {
    people.addMyEntry.mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error('too long'))
    const { result } = renderHook(() => usePersonFacts())
    await waitFor(() => expect(result.current.me).not.toBeNull())
    let ok = false
    await act(async () => { ok = await result.current.add('likes', 'tea', null) })
    expect(ok).toBe(true)
    await act(async () => { ok = await result.current.add('likes', 'x'.repeat(999), null) })
    expect(ok).toBe(false)
    expect(result.current.error).toBe('too long')
  })

  it('pages the audit log by cursor', async () => {
    people.listMyAudit.mockResolvedValueOnce({ events: [{ eventId: 'a1', eventType: 'x' }], nextCursor: 'c1' })
    const { result } = renderHook(() => usePersonFacts())
    await waitFor(() => expect(result.current.nextCursor).toBe('c1'))
    people.listMyAudit.mockResolvedValueOnce({ events: [{ eventId: 'a9', eventType: 'x' }], nextCursor: null })
    let more = false
    await act(async () => { more = await result.current.loadMoreAudit() })
    expect(more).toBe(true)
    expect(people.listMyAudit).toHaveBeenLastCalledWith('c1')
    expect(result.current.audit.map((e) => e.eventId)).toEqual(['a1', 'a9'])
    await act(async () => { more = await result.current.loadMoreAudit() })
    expect(more).toBe(false)
  })
})

describe('FactRow', () => {
  const props = {
    editing: null,
    onEdit: vi.fn(),
    onSave: vi.fn(),
    onForget: vi.fn(),
    busy: null,
    readOnly: false,
    enabled: true,
    names: new Map<string, string>(),
    lens: null,
  }

  it('says who wrote an entry', () => {
    expect(sourceOf(fact()).mine).toBe(true)
    expect(sourceOf(fact({ learned_by: 'ACE', workspace: 'ws' } as Partial<PersonFactDetail>)).text).toMatch(/^ACE/)
  })

  it('offers Correct and Forget, and hides them read-only', () => {
    const onForget = vi.fn()
    render(<ul><FactRow {...props} fact={fact()} onForget={onForget} /></ul>)
    fireEvent.click(screen.getByRole('button', { name: 'Forget' }))
    expect(onForget).toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Correct' })).toBeTruthy()
    cleanup()
    render(<ul><FactRow {...props} readOnly fact={fact()} /></ul>)
    expect(screen.queryByRole('button', { name: 'Forget' })).toBeNull()
  })

  it('edits in place: Escape cancels, Enter saves', () => {
    const onEdit = vi.fn()
    const onSave = vi.fn()
    render(<ul><FactRow {...props} fact={fact()} editing={{ id: 1, text: 'Likes tea' }} onEdit={onEdit} onSave={onSave} /></ul>)
    const box = screen.getByLabelText('Correct this')
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onSave).toHaveBeenCalled()
    fireEvent.keyDown(box, { key: 'Escape' })
    expect(onEdit).toHaveBeenCalledWith(null)
  })
})
