// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PREFS_KEY, RAIL_DEFAULT, RAIL_MAX, loadPrefs, useIdePrefs } from './useIdePrefs'
import { DOORBELL_MIN_MS, UNCONFIRMED_DELETE, useIdeData } from './useIdeData'

const chat = vi.hoisted(() => ({ listSessions: vi.fn() }))
const harness = vi.hoisted(() => ({ listRunners: vi.fn() }))
const agents = vi.hoisted(() => ({ listMyAgents: vi.fn() }))
const socket = vi.hoisted(() => ({ ring: null as null | (() => void), connected: false }))

vi.mock('@/api/chat', async (orig) => ({ ...(await orig<object>()), ...chat }))
vi.mock('@/api/harness', async (orig) => ({ ...(await orig<object>()), ...harness }))
vi.mock('@/api/agents', async (orig) => ({ ...(await orig<object>()), ...agents }))
vi.mock('@/hooks/useReconnectingSocket', () => ({
  useReconnectingSocket: (_url: string, onFrame: () => void) => {
    socket.ring = onFrame
    return { connected: socket.connected }
  },
}))

const session = (id: string, status = 'active') => ({ id, status, last_activity_at: '2026-10-22T00:00:00Z' })

describe('useIdePrefs', () => {
  // jsdom here exposes a `localStorage` whose methods are missing (see
  // NoteComposer.test.tsx), so a plain map stands in, fresh for every test.
  let store: Map<string, string>
  beforeEach(() => {
    store = new Map()
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    })
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('falls back to defaults, and survives a corrupt entry', () => {
    expect(loadPrefs().railWidth).toBe(RAIL_DEFAULT)
    window.localStorage.setItem(PREFS_KEY, '{nope')
    expect(loadPrefs().railHidden).toBe(false)
  })

  it('merges a partial saved entry over the defaults and clamps the width', () => {
    window.localStorage.setItem(PREFS_KEY, JSON.stringify({ railWidth: 5000, filters: { archived: true }, collapsed: ['a'] }))
    const p = loadPrefs()
    expect(p.railWidth).toBe(RAIL_MAX)
    expect(p.filters.archived).toBe(true)
    expect(p.filters.states.running).toBe(true)
    expect(p.collapsed).toEqual(['a'])
  })

  it('writes every change back, but never the search text', () => {
    const { result } = renderHook(() => useIdePrefs())
    act(() => {
      result.current.setRailHidden(true)
      result.current.setCollapsed(new Set(['repo']))
      result.current.setFilters((f) => ({ ...f, query: 'secret' }))
    })
    const saved = JSON.parse(window.localStorage.getItem(PREFS_KEY) ?? '{}')
    expect(saved.railHidden).toBe(true)
    expect(saved.collapsed).toEqual(['repo'])
    expect(JSON.stringify(saved)).not.toContain('secret')
    const again = renderHook(() => useIdePrefs())
    expect(again.result.current.railHidden).toBe(true)
    expect(again.result.current.filters.query).toBe('')
  })
})

describe('useIdeData', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    socket.ring = null
    chat.listSessions.mockResolvedValue([session('s1')])
    harness.listRunners.mockResolvedValue([{ id: 'r1' }])
    agents.listMyAgents.mockResolvedValue({ items: [{ slug: 'ada' }] })
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('loads sessions (to the list ceiling), runners and agents', async () => {
    const { result } = renderHook(() => useIdeData('active'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(chat.listSessions).toHaveBeenCalledWith('active', { limit: 500 })
    expect(result.current.sessions.map((s) => s.id)).toEqual(['s1'])
    expect(result.current.runners).toHaveLength(1)
    await waitFor(() => expect(result.current.agents).toHaveLength(1))
  })

  it('reports a failed session read and keeps runners', async () => {
    chat.listSessions.mockRejectedValue(new Error('down'))
    const { result } = renderHook(() => useIdeData('all'))
    await waitFor(() => expect(result.current.error).toBe('down'))
    expect(result.current.runners).toHaveLength(1)
  })

  it('a doorbell ring refetches, at most once per window', async () => {
    const { result } = renderHook(() => useIdeData('active'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    vi.useFakeTimers()
    const calls = chat.listSessions.mock.calls.length
    act(() => {
      socket.ring?.()
      socket.ring?.()
      socket.ring?.()
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(DOORBELL_MIN_MS) })
    expect(chat.listSessions.mock.calls.length).toBe(calls + 1)
  })

  it('says a relayed delete went unconfirmed, then takes it back once it lands', async () => {
    const { result } = renderHook(() => useIdeData('active'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    // Asked long ago: the next report that still lists it calls it stuck.
    act(() => result.current.setClosing({ s1: 0 }))
    await act(async () => { await result.current.refresh() })
    expect(result.current.note).toEqual({ text: UNCONFIRMED_DELETE, error: true })
    chat.listSessions.mockResolvedValue([])
    await act(async () => { await result.current.refresh() })
    expect(result.current.note).toEqual({ text: 'Deleted', error: false })
  })

  it('replaces "Deleting…" with "Deleted" once the runner retires the row', async () => {
    const { result } = renderHook(() => useIdeData('active'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.trackClose('s1', 'Deleting “IDE” on jj-mbp-cdp…', 'Deleted “IDE”'))
    expect(result.current.note?.text).toBe('Deleting “IDE” on jj-mbp-cdp…')
    expect(Object.keys(result.current.closing)).toEqual(['s1'])
    // Still listed: the line keeps saying it is in hand.
    await act(async () => { await result.current.refresh() })
    expect(result.current.note?.text).toBe('Deleting “IDE” on jj-mbp-cdp…')
    // The runner's report retires it: the line says so, and nothing is pending.
    chat.listSessions.mockResolvedValue([])
    await act(async () => { await result.current.refresh() })
    expect(result.current.note).toEqual({ text: 'Deleted “IDE”', error: false })
    expect(result.current.closing).toEqual({})
  })

  it('a delete the runner never confirms turns "Deleting…" into an error, not a standing line', async () => {
    const { result } = renderHook(() => useIdeData('active'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    vi.useFakeTimers({ toFake: ['Date'] })
    act(() => result.current.trackClose('s1', 'Deleting “IDE” on jj-mbp-cdp…', 'Deleted “IDE”'))
    vi.setSystemTime(Date.now() + 46_000)
    await act(async () => { await result.current.refresh() })
    expect(result.current.note).toEqual({ text: UNCONFIRMED_DELETE, error: true })
    expect(result.current.closing).toEqual({})
  })

  it('a different line written since is left alone when the delete lands', async () => {
    const { result } = renderHook(() => useIdeData('active'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.trackClose('s1', 'Deleting…', 'Deleted “IDE”'))
    act(() => result.current.setNote({ text: 'Archived “other”', error: false }))
    chat.listSessions.mockResolvedValue([])
    await act(async () => { await result.current.refresh() })
    expect(result.current.note).toEqual({ text: 'Archived “other”', error: false })
  })
})
