// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { signOff } from './signOff'
import { SOURCE } from './protocol'

const CANOPY = 'https://canopy.example'
const ID = 'so_abcdefghijklmnop'

function harness(overrides: Record<string, unknown> = {}) {
  const posted: Array<{ message: Record<string, unknown>; targetOrigin: string }> = []
  const outcome = signOff({ baseUrl: CANOPY, app: 'connect-labs', tokenUrl: '/canopy/token/', id: ID,
                            ...overrides } as Parameters<typeof signOff>[0])
  const host = document.querySelector('[data-canopy-sign-off]') as HTMLElement
  const iframe = host.shadowRoot!.querySelector('iframe') as HTMLIFrameElement
  // As widget.test.ts: jsdom gives a shadow-root iframe no window, so stand one in.
  const frameWindow = { postMessage: (message: Record<string, unknown>, targetOrigin: string) => posted.push({ message, targetOrigin }) }
  Object.defineProperty(iframe, 'contentWindow', { value: frameWindow, configurable: true })
  async function fromFrame(data: unknown, opts: { origin?: string; source?: unknown } = {}) {
    window.dispatchEvent(new MessageEvent('message', {
      data, origin: opts.origin ?? CANOPY,
      source: (opts.source === undefined ? frameWindow : opts.source) as Window,
    }))
    await new Promise((r) => setTimeout(r, 0))
  }
  return { outcome, host, iframe, posted, fromFrame }
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ token: 'frame-tok', expires_at: 'later' }) })))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('canopy.signOff', () => {
  it('frames canopy on the one request, allowed to ask for a passkey, with no launcher', () => {
    const { host, iframe } = harness({ agent: 'labsdata', theme: { mode: 'light' } })
    expect(iframe.src).toBe(`${CANOPY}/embed/sign-off?app=connect-labs&sign_off=${ID}&agent=labsdata&theme=light`)
    expect(iframe.getAttribute('allow')).toBe('publickey-credentials-get')
    expect(host.shadowRoot!.querySelector('.launcher')).toBeNull()
    expect(host.shadowRoot!.querySelector('[role=dialog]')?.getAttribute('aria-modal')).toBe('true')
  })

  it('hands the frame a token from the host on ready, to canopy only', async () => {
    const { fromFrame, posted } = harness()
    await fromFrame({ source: SOURCE, type: 'ready' })
    const init = posted.find((p) => p.message.type === 'init')!
    expect(init.message.token).toBe('frame-tok')
    expect(init.targetOrigin).toBe(CANOPY)
    const [, request] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    // Never an act-as-you permission: a sign-off is not a conversation.
    expect(request.body).toBe('{}')
  })

  it('resolves with the decision and takes the dialog away', async () => {
    const { fromFrame, outcome } = harness()
    await fromFrame({ source: SOURCE, type: 'sign-off', id: ID, state: 'approved' })
    await expect(outcome).resolves.toEqual({ id: ID, state: 'approved' })
    expect(document.querySelector('[data-canopy-sign-off]')).toBeNull()
  })

  it('is closed undecided by the frame, Escape, or the backdrop', async () => {
    const first = harness()
    await first.fromFrame({ source: SOURCE, type: 'close' })
    await expect(first.outcome).resolves.toEqual({ id: ID, state: 'closed' })

    const second = harness()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await expect(second.outcome).resolves.toEqual({ id: ID, state: 'closed' })

    const third = harness()
    ;(third.host.shadowRoot!.querySelector('.backdrop') as HTMLElement).click()
    await expect(third.outcome).resolves.toEqual({ id: ID, state: 'closed' })
  })

  it('ignores another origin, another window, and another request', async () => {
    const { fromFrame, host } = harness()
    await fromFrame({ source: SOURCE, type: 'sign-off', id: ID, state: 'approved' }, { origin: 'https://evil.example' })
    await fromFrame({ source: SOURCE, type: 'sign-off', id: ID, state: 'approved' }, { source: window })
    await fromFrame({ source: SOURCE, type: 'sign-off', id: 'so_someoneelsesrequest', state: 'approved' })
    await fromFrame({ source: SOURCE, type: 'sign-off', id: ID, state: 'granted' })
    expect(host.isConnected).toBe(true)
  })

  it('refuses something that is not a sign-off id, before framing anything', async () => {
    await expect(signOff({ baseUrl: CANOPY, app: 'connect-labs', tokenUrl: '/t/', id: '../chat' })).rejects.toThrow(/so_/)
    expect(document.querySelector('[data-canopy-sign-off]')).toBeNull()
  })
})
