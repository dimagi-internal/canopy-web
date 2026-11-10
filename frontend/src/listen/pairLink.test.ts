// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { captureListenPairing, isExpired, parsePairFragment, takeStashedPairing } from './pairLink'
import { b64u } from './relayCrypto'

const KEY = b64u(new Uint8Array([4, ...new Array(64).fill(17)]))
const SECRET = b64u(new Uint8Array(32).fill(9))

function fragment(fields: Record<string, unknown>): string {
  return '#pair=' + b64u(new TextEncoder().encode(JSON.stringify(fields)))
}

const GOOD = { r: 'runner-1', k: KEY, s: SECRET, n: "Jonathan's MacBook", x: 2_000_000_000 }

afterEach(() => {
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('parsePairFragment', () => {
  it('reads the QR link', () => {
    const invite = parsePairFragment(fragment(GOOD))!
    expect(invite.runnerId).toBe('runner-1')
    expect(invite.laptopPub).toBe(KEY)
    expect(invite.secret).toHaveLength(32)
    expect(invite.laptopName).toBe("Jonathan's MacBook")
    expect(isExpired(invite, 1_999_999_999_000)).toBe(false)
    expect(isExpired(invite, 2_000_000_000_000)).toBe(true)
  })

  it.each([
    ['not a pair link', '#other=1'],
    ['garbage', '#pair=!!!'],
    ['short secret', fragment({ ...GOOD, s: b64u(new Uint8Array(16)) })],
    ['bad key', fragment({ ...GOOD, k: b64u(new Uint8Array(65)) })],
    ['no expiry', fragment({ ...GOOD, x: undefined })],
  ])('refuses %s', (_why, hash) => {
    expect(parsePairFragment(hash)).toBeNull()
  })
})

describe('captureListenPairing', () => {
  it('takes the secret out of the address at once and hands it over once', () => {
    const hash = fragment(GOOD)
    window.history.replaceState(null, '', `/listen?x=1${hash}`)
    captureListenPairing(window, '/')
    expect(window.location.hash).toBe('')
    expect(window.location.pathname + window.location.search).toBe('/listen?x=1')
    expect(takeStashedPairing()).toBe(hash)
    expect(takeStashedPairing()).toBeNull()
  })

  it('leaves every other page alone', () => {
    window.history.replaceState(null, '', `/home${fragment(GOOD)}`)
    captureListenPairing(window, '/')
    expect(window.location.hash).not.toBe('')
    expect(takeStashedPairing()).toBeNull()
  })
})
