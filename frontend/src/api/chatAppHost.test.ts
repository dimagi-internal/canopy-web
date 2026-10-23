// @vitest-environment jsdom
// The chat page's MCP Apps host: a View's resources/subscribe reaches canopy's
// route as the person looking, and the host's update (relayed on the socket as
// `app.resource_updated`) reaches the listeners its Views registered.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { appResourceUpdate, createAppResourceUpdates } from 'canopy-ui/chat'

import { appHostFor } from './chat'

afterEach(() => vi.unstubAllGlobals())

describe('appHostFor', () => {
  it('subscribes and unsubscribes through the session routes', async () => {
    const fetch = vi.fn(async () => new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetch)
    const host = appHostFor('s1')
    await host.subscribe!('toolu_1', 'labs-result://r1')
    await host.unsubscribe!('toolu_1', 'labs-result://r1')
    const calls = fetch.mock.calls as unknown as Array<[string, RequestInit]>
    expect(calls.map(([url]) => String(url))).toEqual([
      expect.stringContaining('/api/canopy-sessions/s1/apps/toolu_1/subscribe'),
      expect.stringContaining('/api/canopy-sessions/s1/apps/toolu_1/unsubscribe'),
    ])
    expect(JSON.parse(String(calls[0][1].body))).toEqual({ uri: 'labs-result://r1' })
  })

  it("fans a socket's app.resource_updated frame out to the listeners", () => {
    const updates = createAppResourceUpdates()
    const host = appHostFor('s1', updates)
    const heard: string[] = []
    const stop = host.onResourceUpdated!((callId, uri) => heard.push(`${callId} ${uri}`))
    const frame = { event: 'app.resource_updated', data: { tool_call_id: 'toolu_1', uri: 'labs-result://r1' } }
    const update = appResourceUpdate(frame)!
    updates.emit(update.toolCallId, update.uri)
    expect(appResourceUpdate({ event: 'page.invalidate', data: { uri: 'x' } })).toBeNull()
    stop()
    updates.emit('toolu_1', 'labs-result://r1')
    expect(heard).toEqual(['toolu_1 labs-result://r1'])
  })
})
