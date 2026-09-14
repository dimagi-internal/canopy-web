import { describe, expect, it } from 'vitest'

import { frameBaseUrl } from './frameBase'

describe('the frame\'s view of where canopy is', () => {
  it('carries the deployment path prefix, not just the origin', () => {
    // THE bug. `window.location.origin` alone sent every call from inside the
    // frame to `https://labs.connect.dimagi.com/api/...`, which is the ROOT
    // tenant on that host — connect-labs — and not canopy at all.
    expect(frameBaseUrl('https://labs.connect.dimagi.com', '/canopy/')).toBe(
      'https://labs.connect.dimagi.com/canopy',
    )
  })

  it('adds nothing at a root deployment', () => {
    expect(frameBaseUrl('https://canopy.example.com', '/')).toBe('https://canopy.example.com')
  })

  it('leaves a prefix without a trailing slash alone', () => {
    expect(frameBaseUrl('https://x.test', '/canopy')).toBe('https://x.test/canopy')
  })
})
