// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createHostLink } from './hostLink'

const HOST = 'https://labs.connect.dimagi.com'
const SOURCE = 'canopy-widget'

/**
 * The frame's half of the bridge. The host is simulated the way a real one
 * behaves: it receives what the frame posts to `window.parent`, and replies by
 * dispatching `message` events with an origin.
 */
const links: Array<{ dispose(): void }> = []

function harness(origins: string[] = [HOST]) {
  const posted: Array<{ message: Record<string, unknown>; targetOrigin: string }> = []

  // `window.parent === window` in jsdom, so posting to the parent is
  // observable by spying on this window.
  vi.spyOn(window.parent, 'postMessage').mockImplementation(((
    message: Record<string, unknown>,
    targetOrigin: string,
  ) => {
    posted.push({ message, targetOrigin })
  }) as typeof window.parent.postMessage)

  const link = createHostLink({ app: 'connect-labs', origins })
  links.push(link)

  function fromHost(data: unknown, origin = HOST) {
    window.dispatchEvent(new MessageEvent('message', { data, origin }))
    return new Promise((r) => setTimeout(r, 0))
  }

  const sent = (type: string) => posted.filter((p) => p.message.type === type)

  return { link, posted, sent, fromHost }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

afterEach(() => {
  // Every link registers a window listener. Left attached, a link from an
  // earlier test still hears later messages — which is exactly how the
  // cross-link request-id collision showed up.
  while (links.length) links.pop()!.dispose()
  vi.restoreAllMocks()
})

describe('announcing itself', () => {
  it('posts ready immediately, carrying nothing', () => {
    const { sent } = harness()
    const [ready] = sent('ready')
    expect(ready.message).toEqual({ source: SOURCE, type: 'ready' })
  })

  it('posts ready to * because the parent origin is not knowable yet', () => {
    // Safe for exactly this message: it is "I exist" and carries no data. The
    // host replies to the frame's own known origin, and everything after is
    // sent to an origin already checked against the list.
    const { sent } = harness()
    expect(sent('ready')[0].targetOrigin).toBe('*')
  })
})

describe('origin validation', () => {
  it('ignores a message from an origin the SERVER did not list', async () => {
    const { link, fromHost } = harness([HOST])
    const init = link.waitForInit()
    let settled = false
    init.then(
      () => {
        settled = true
      },
      () => undefined,
    )

    await fromHost({ source: SOURCE, type: 'init', token: 'attacker', actions: [] }, 'https://evil.example')

    expect(settled).toBe(false)
  })

  it('accepts any of several listed origins — staging and prod both embed', async () => {
    const staging = 'https://labs-staging.dimagi.com'
    const { link, fromHost } = harness([HOST, staging])
    const init = link.waitForInit()

    await fromHost({ source: SOURCE, type: 'init', token: 'tok', actions: [] }, staging)

    await expect(init).resolves.toMatchObject({ token: 'tok' })
  })

  it('ignores messages that are not ours', async () => {
    const { link, fromHost } = harness()
    const init = link.waitForInit()
    let settled = false
    init.then(
      () => {
        settled = true
      },
      () => undefined,
    )

    await fromHost({ type: 'init', token: 'x', actions: [] })
    await fromHost({ source: 'other-widget', type: 'init', token: 'x', actions: [] })
    await fromHost('a string')
    await fromHost(null)

    expect(settled).toBe(false)
  })

  it('replies to the host origin, never to *', async () => {
    const { link, fromHost, sent } = harness()
    await fromHost({ source: SOURCE, type: 'init', token: 'tok', actions: [] })

    // Deliberately left unanswered — only the targetOrigin is under test — so
    // it rejects at teardown and needs a handler now.
    link.requestContext().catch(() => undefined)
    await new Promise((r) => setTimeout(r, 0))

    const [request] = sent('context-request')
    expect(request.targetOrigin).toBe(HOST)
  })
})

describe('the handshake', () => {
  it('resolves with the token, agent and actions the host sent', async () => {
    const { link, fromHost } = harness()
    const init = link.waitForInit()

    await fromHost({
      source: SOURCE,
      type: 'init',
      token: 'tok',
      agent: 'labs-helper',
      metadata: { supplyPoint: 7 },
      actions: [{ name: 'recordCount' }],
    })

    await expect(init).resolves.toEqual({
      token: 'tok',
      agent: 'labs-helper',
      metadata: { supplyPoint: 7 },
      actions: [{ name: 'recordCount' }],
    })
  })

  it('rejects when the host could not mint, so the UI can say why', async () => {
    // Otherwise the frame spins on "Starting…" and the user has nothing to act
    // on — the exact failure the host's token-error message exists to prevent.
    const { link, fromHost } = harness()
    const assertion = expect(link.waitForInit()).rejects.toThrow('403')

    await fromHost({ source: SOURCE, type: 'token-error', id: 'init', message: 'token endpoint returned 403' })

    await assertion
  })
})

describe('requests', () => {
  async function connected() {
    const h = harness()
    await h.fromHost({ source: SOURCE, type: 'init', token: 'tok', actions: [{ name: 'act' }] })
    return h
  }

  it('resolves a token request with the host reply, matched by id', async () => {
    const { link, fromHost, sent } = await connected()
    const promise = link.requestToken()
    await new Promise((r) => setTimeout(r, 0))
    const id = sent('token-request')[0].message.id as string

    await fromHost({ source: SOURCE, type: 'token', id, token: 'fresh', expires_at: 'later' })

    await expect(promise).resolves.toEqual({ token: 'fresh', expiresAt: 'later' })
  })

  it('does not settle a request with another request0s reply', async () => {
    const { link, fromHost, sent } = await connected()
    const first = link.requestContext()
    // Handled the moment it exists: it is deliberately left outstanding and
    // then rejected at teardown, which node reports as unhandled otherwise.
    let firstSettled = false
    first.then(
      () => {
        firstSettled = true
      },
      () => undefined,
    )
    const second = link.requestContext()
    await new Promise((r) => setTimeout(r, 0))
    const ids = sent('context-request').map((p) => p.message.id as string)
    expect(new Set(ids).size).toBe(2)

    await fromHost({ source: SOURCE, type: 'context', id: ids[1], context: { second: true } })

    await expect(second).resolves.toEqual({ second: true })
    // The first is still outstanding, not resolved with the second's payload.
    await new Promise((r) => setTimeout(r, 0))
    expect(firstSettled).toBe(false)
  })

  it('rejects an action the host refused, with the host0s reason', async () => {
    const { link, fromHost, sent } = await connected()
    const promise = link.runAction('act', { qty: 3 })
    await new Promise((r) => setTimeout(r, 0))
    const request = sent('action-request')[0].message
    expect(request).toMatchObject({ name: 'act', args: { qty: 3 } })

    const assertion = expect(promise).rejects.toThrow('not allowed on a completed count')
    await fromHost({
      source: SOURCE,
      type: 'action-error',
      id: request.id as string,
      message: 'not allowed on a completed count',
    })
    await assertion
  })

  it('tracks the action list as the host changes it, schemas and all', async () => {
    const { link, fromHost } = await connected()
    expect(link.actions().map((a) => a.name)).toEqual(['act'])

    const seen: string[][] = []
    link.onActionsChanged((specs) => seen.push(specs.map((s) => s.name)))
    await fromHost({
      source: SOURCE,
      type: 'actions',
      actions: [
        { name: 'act' },
        {
          name: 'other',
          description: 'Do the other thing',
          parameters: { type: 'object', properties: { n: { type: 'integer' } } },
        },
      ],
    })

    expect(link.actions().map((a) => a.name)).toEqual(['act', 'other'])
    expect(seen).toEqual([['act', 'other']])
    // The SCHEMA is what canopy turns into an MCP tool, so losing it here
    // would leave the agent with a tool it cannot call.
    expect(link.actions()[1].parameters).toMatchObject({ type: 'object' })
  })

  it('refuses to ask before the host has spoken', async () => {
    // There is no origin to post to yet, and posting to * would leak the
    // request to whatever is above us.
    const { link } = harness()
    await expect(link.requestContext()).rejects.toThrow('has not spoken')
  })
})

describe('the page state channel', () => {
  it('receives what the host pushes, and keeps receiving', async () => {
    // Pushed, not polled — the whole difference from `requestContext`, which is
    // answered once and then cannot say the page moved.
    const { link, fromHost } = harness()
    const seen: unknown[] = []
    link.onPageStateChanged((s) => seen.push(s))

    await fromHost({ source: SOURCE, type: 'state', state: { visible_ids: [1, 2] } })
    await fromHost({ source: SOURCE, type: 'state', state: { visible_ids: [2] } })

    expect(seen).toEqual([{ visible_ids: [1, 2] }, { visible_ids: [2] }])
    expect(link.pageState()).toEqual({ visible_ids: [2] })
  })

  it('reads as null until the host speaks, not as an empty view', async () => {
    // A host that does not use the channel must not be read as one declaring
    // the user's screen blank — that would overwrite nothing with an assertion.
    const { link } = harness()

    expect(link.pageState()).toBeNull()
  })

  it('ignores a push from an origin the server did not name', async () => {
    const { link, fromHost } = harness()

    await fromHost({ source: SOURCE, type: 'state', state: { visible_ids: [9] } }, 'https://evil.example')

    expect(link.pageState()).toBeNull()
  })

  it('drops a malformed push rather than blanking a good view', async () => {
    const { link, fromHost } = harness()
    await fromHost({ source: SOURCE, type: 'state', state: { visible_ids: [1] } })

    await fromHost({ source: SOURCE, type: 'state', state: 'not an object' })
    await fromHost({ source: SOURCE, type: 'state', state: ['also', 'not'] })
    await fromHost({ source: SOURCE, type: 'state' })

    expect(link.pageState()).toEqual({ visible_ids: [1] })
  })

  it('stops notifying once the listener unsubscribes', async () => {
    const { link, fromHost } = harness()
    const seen: unknown[] = []
    const stop = link.onPageStateChanged((s) => seen.push(s))

    stop()
    await fromHost({ source: SOURCE, type: 'state', state: { visible_ids: [3] } })

    expect(seen).toEqual([])
    // Still RECORDED, though — a late reader must get the current view.
    expect(link.pageState()).toEqual({ visible_ids: [3] })
  })
})
