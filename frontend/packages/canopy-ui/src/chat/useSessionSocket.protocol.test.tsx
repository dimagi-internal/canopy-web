// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useSessionSocket, withAguiProtocol } from './useSessionSocket'

/**
 * What the socket REALLY opens, and what it does with what comes back.
 *
 * This replaced an assertion that the hook's source contained `protocol=ag-ui`.
 * That was true the whole time the flag was being dropped: the hook put it on
 * the PATH it handed the caller's URL builder, and two of the first three
 * builders ignore their path. Only opening a socket and reading its URL can see
 * that — so that is what these do.
 */

class FakeSocket {
  static opened: FakeSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  readyState = 0
  constructor(public url: string) {
    FakeSocket.opened.push(this)
  }
  send() {}
  close() {}
  receive(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }
}

beforeEach(() => {
  FakeSocket.opened = []
  vi.stubGlobal('WebSocket', FakeSocket)
})
afterEach(() => vi.unstubAllGlobals())

function lastUrl(): string {
  return FakeSocket.opened.at(-1)!.url
}

describe('the flag survives whatever URL the caller builds', () => {
  it('with a builder that uses the path it is given', () => {
    renderHook(() =>
      useSessionSocket({ sessionId: 's1', wsUrl: (p) => `wss://h/${p}`, protocol: 'ag-ui' }),
    )
    expect(lastUrl()).toBe('wss://h/ws/canopy-sessions/s1/?protocol=ag-ui')
  })

  it('with a builder that IGNORES its path — canopy-web’s widget', () => {
    // `() => client.sessionSocketUrl(sessionId)`: the widget's real shape.
    renderHook(() =>
      useSessionSocket({ sessionId: 's1', wsUrl: () => 'wss://h/ws/canopy-sessions/s1/', protocol: 'ag-ui' }),
    )
    expect(lastUrl()).toContain('protocol=ag-ui')
  })

  it('with a builder that carries its own token — ace-web', () => {
    // `buildCanopyWsUrl(base, id)` puts a token in the query, so the flag has
    // to JOIN the query rather than start a second one.
    renderHook(() =>
      useSessionSocket({ sessionId: 's1', wsUrl: () => 'wss://h/ws/canopy-sessions/s1/?token=abc', protocol: 'ag-ui' }),
    )
    expect(lastUrl()).toBe('wss://h/ws/canopy-sessions/s1/?token=abc&protocol=ag-ui')
  })

  it('asks for nothing when the caller asks for nothing', () => {
    // The default is canopy's own frames, which is what an un-upgraded consumer
    // must keep getting.
    renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl: () => 'wss://h/x/?token=abc' }))
    expect(lastUrl()).toBe('wss://h/x/?token=abc')
  })
})

describe('withAguiProtocol', () => {
  it('does not add the flag twice', () => {
    expect(withAguiProtocol('wss://h/x/?protocol=ag-ui')).toBe('wss://h/x/?protocol=ag-ui')
  })

  it('leaves an empty URL empty — the caller’s "not yet"', () => {
    expect(withAguiProtocol('')).toBe('')
  })
})

describe('a server that answers in canopy frames anyway', () => {
  it('is understood, not decoded to nothing', () => {
    // Asked for AG-UI, got native: an old server, or a flag lost on the way.
    // Decoding these as AG-UI yields zero frames — a blank chat with no error.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const onTitleUpdated = vi.fn()
    renderHook(() =>
      useSessionSocket({ sessionId: 's1', wsUrl: (p) => `wss://h/${p}`, protocol: 'ag-ui', onTitleUpdated }),
    )

    act(() => {
      FakeSocket.opened.at(-1)!.receive({
        event: 'session.title_updated',
        data: { title: 'Still readable' },
      })
    })

    expect(onTitleUpdated).toHaveBeenCalledTimes(1)
    expect(warn).toHaveBeenCalledTimes(1)
    warn.mockRestore()
  })

  it('still decodes real AG-UI events as AG-UI', () => {
    const onTitleUpdated = vi.fn()
    renderHook(() =>
      useSessionSocket({ sessionId: 's1', wsUrl: (p) => `wss://h/${p}`, protocol: 'ag-ui', onTitleUpdated }),
    )

    act(() => {
      FakeSocket.opened.at(-1)!.receive({
        type: 'STATE_DELTA',
        delta: [{ op: 'replace', path: '/title', value: 'Via AG-UI' }],
      })
    })

    expect(onTitleUpdated).toHaveBeenCalledTimes(1)
  })
})
