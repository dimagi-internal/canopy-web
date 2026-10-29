// @vitest-environment jsdom
// Where a passkey can be used, and where the person is sent when it cannot: the
// Canopy Runner app (its user agent, or a token-minted session) hands every passkey
// moment to the browser, at a URL under the deployment's prefix.
import { afterEach, describe, expect, it } from 'vitest'
import {
  browserUrl,
  inRunnerApp,
  passkeysWorkHere,
  PasskeyInBrowserError,
  requirePasskeysHere,
  setSessionPasskeys,
} from './passkeyHere'

afterEach(() => setSessionPasskeys(true))

describe('passkeyHere', () => {
  it('knows the Runner app by its user agent', () => {
    expect(inRunnerApp('Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 (KHTML, like Gecko) CanopyRunner/2.0')).toBe(true)
    expect(inRunnerApp('Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Safari/537.36')).toBe(false)
  })

  it('a token-minted session cannot use a passkey, whatever the browser', () => {
    expect(passkeysWorkHere()).toBe(true)
    setSessionPasskeys(false)
    expect(passkeysWorkHere()).toBe(false)
    expect(() => requirePasskeysHere()).toThrow(PasskeyInBrowserError)
  })

  it('puts an app route under the deployment prefix and keeps a full URL', () => {
    const origin = window.location.origin
    expect(browserUrl('/act-as/abc', '/canopy/')).toBe(`${origin}/canopy/act-as/abc`)
    expect(browserUrl('/act-as/abc', '/')).toBe(`${origin}/act-as/abc`)
    expect(browserUrl('https://canopy.dimagi.com/x?y=1', '/canopy/')).toBe('https://canopy.dimagi.com/x?y=1')
  })
})
