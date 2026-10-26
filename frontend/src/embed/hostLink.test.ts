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
      // An older loader sends no expiry: "" (the client treats it as expired).
      expiresAt: '',
      agent: 'labs-helper',
      metadata: { supplyPoint: 7 },
      actions: [{ name: 'recordCount' }],
      // An older loader says nothing about expanding: not expandable.
      expandable: false,
      expanded: false,
      snappable: false,
      snapped: false,
    })
  })

  it('follows the host on whether the panel is expanded, and asks it to change', async () => {
    const { link, fromHost, sent } = harness()
    const seen: boolean[] = []
    link.onDisplayChanged((e) => seen.push(e))
    await fromHost({ source: SOURCE, type: 'init', token: 'tok', actions: [],
                     expandable: true, expanded: true })
    await expect(link.waitForInit()).resolves.toMatchObject({ expandable: true, expanded: true })
    expect(link.isExpanded()).toBe(true)
    link.requestExpand(false)
    expect(sent('expand')[0]).toMatchObject({ message: { type: 'expand', expanded: false },
                                              targetOrigin: HOST })
    await fromHost({ source: SOURCE, type: 'display', expanded: false })
    expect(link.isExpanded()).toBe(false)
    expect(seen).toEqual([false])
  })

  it('follows the host on whether the rail is snapped, and asks it to change', async () => {
    const { link, fromHost, sent } = harness()
    const seen: boolean[] = []
    link.onSnapChanged((v) => seen.push(v))
    await fromHost({ source: SOURCE, type: 'init', token: 'tok', actions: [],
                     snappable: true, snapped: false })
    await expect(link.waitForInit()).resolves.toMatchObject({ snappable: true, snapped: false })
    link.requestSnap(true)
    expect(sent('snap')[0]).toMatchObject({ message: { type: 'snap', snapped: true },
                                            targetOrigin: HOST })
    await fromHost({ source: SOURCE, type: 'display', expanded: false, snapped: true })
    expect(link.isSnapped()).toBe(true)
    // An older loader's `display` says nothing about snapping: unchanged.
    await fromHost({ source: SOURCE, type: 'display', expanded: true })
    expect(link.isSnapped()).toBe(true)
    expect(seen).toEqual([true])
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
    // A plain request never asks the host for an act-as-you permission.
    expect(sent('token-request')[0].message.grant).toBe(false)
  })

  it('asks for an act-as-you permission only when told to', async () => {
    const { link, sent } = await connected()
    void link.requestToken({ grant: true }).catch(() => undefined)
    await new Promise((r) => setTimeout(r, 0))
    expect(sent('token-request')[0].message.grant).toBe(true)
    link.dispose()
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

describe('the host theme', () => {
  it('rides init unvalidated, for applyFrameTheme to check itself', async () => {
    const { link, fromHost } = harness()
    fromHost({ source: SOURCE, type: 'init', token: 't', actions: [], theme: { mode: 'light' } })
    expect((await link.waitForInit()).theme).toEqual({ mode: 'light' })
  })

  it('delivers a live re-theme to listeners', () => {
    const { link, fromHost } = harness()
    fromHost({ source: SOURCE, type: 'init', token: 't', actions: [] })
    const seen: unknown[] = []
    link.onThemeChanged((t) => seen.push(t))
    fromHost({ source: SOURCE, type: 'theme', theme: { mode: 'dark' } })
    expect(seen).toEqual([{ mode: 'dark' }])
  })

  it('ignores a theme from an origin that is not the host', () => {
    const { link, fromHost } = harness()
    fromHost({ source: SOURCE, type: 'init', token: 't', actions: [] })
    const seen: unknown[] = []
    link.onThemeChanged((t) => seen.push(t))
    fromHost({ source: SOURCE, type: 'theme', theme: { accent: '#000' } }, 'https://evil.example')
    expect(seen).toEqual([])
  })
})

describe('a question the page asks', () => {
  it('reaches a listener, trimmed', async () => {
    const { link, fromHost } = harness()
    const seen: string[] = []
    link.onAsk((t) => seen.push(t))

    await fromHost({ source: SOURCE, type: 'ask', text: '  Explain this answer.  ' })

    expect(seen).toEqual(['Explain this answer.'])
  })

  it('is held for the first listener when it arrives before the app is listening', async () => {
    const { link, fromHost } = harness()
    await fromHost({ source: SOURCE, type: 'ask', text: 'Explain this answer.' })

    const seen: string[] = []
    link.onAsk((t) => seen.push(t))
    // Handed over once, not again to a later listener.
    const later: string[] = []
    link.onAsk((t) => later.push(t))

    expect(seen).toEqual(['Explain this answer.'])
    expect(later).toEqual([])
  })

  it('drops an empty or non-text question, and one from an origin the server did not name', async () => {
    const { link, fromHost } = harness()
    const seen: string[] = []
    link.onAsk((t) => seen.push(t))

    await fromHost({ source: SOURCE, type: 'ask', text: '   ' })
    await fromHost({ source: SOURCE, type: 'ask', text: 42 })
    await fromHost({ source: SOURCE, type: 'ask', text: 'Explain.' }, 'https://evil.example.com')

    expect(seen).toEqual([])
  })
})

describe('the conversation open in the panel', () => {
  const ID = '0f8fad5b-d9cb-469f-a165-70867728950e'

  it('hands a request to open a conversation to the first listener, held until then', async () => {
    const { link, fromHost } = harness()
    await fromHost({ source: SOURCE, type: 'open-session', id: ID })
    const heard: string[] = []
    link.onOpenSession((id) => heard.push(id))
    expect(heard).toEqual([ID])
    await fromHost({ source: SOURCE, type: 'open-session', id: ID })
    expect(heard).toEqual([ID, ID])
  })

  it('drops an id that is not a session id, and one from an origin the server did not name', async () => {
    const { link, fromHost } = harness()
    const heard: string[] = []
    link.onOpenSession((id) => heard.push(id))
    await fromHost({ source: SOURCE, type: 'open-session', id: '../me' })
    await fromHost({ source: SOURCE, type: 'open-session', id: ID }, 'https://evil.example')
    expect(heard).toEqual([])
  })

  it('reports exactly three fields to the host origin, and only when they change', async () => {
    const { link, sent, fromHost } = harness()
    // Before the host has spoken there is nobody to tell.
    link.reportSession(null)
    expect(sent('session')).toHaveLength(0)
    await fromHost({ source: SOURCE, type: 'theme', theme: {} })

    const extra = { id: ID, resource: 'labs-data-space://7', private: true, title: 'never sent' }
    link.reportSession(extra)
    link.reportSession({ ...extra })
    expect(sent('session')).toHaveLength(1)
    expect(sent('session')[0].message.session).toEqual({ id: ID, resource: 'labs-data-space://7', private: true })
    expect(sent('session')[0].targetOrigin).toBe(HOST)

    link.reportSession(null)
    expect(sent('session').map((p) => p.message.session)).toEqual([
      { id: ID, resource: 'labs-data-space://7', private: true }, null,
    ])
  })
})
