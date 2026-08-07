import { describe, expect, it } from 'vitest'
import { loginHref } from './loginHref'

describe('loginHref', () => {
  it('points at the Google OAuth entry under the deployment prefix', () => {
    // BASE_URL is "/" under vitest, so the prefix collapses to "" — the
    // trailing-slash strip is what keeps this from becoming "//accounts/…".
    expect(loginHref('/walkthrough/abc')).toBe(
      '/accounts/google/login/?next=%2Fwalkthrough%2Fabc',
    )
  })

  it('encodes the next target so a query string survives the round trip', () => {
    // The share token rides in ?t=; an unencoded & would truncate `next` at the
    // first param and drop the visitor somewhere they did not ask for.
    const href = loginHref('/walkthrough/abc?t=tok&x=1')
    expect(href).toContain('next=%2Fwalkthrough%2Fabc%3Ft%3Dtok%26x%3D1')
    expect(href.split('?')).toHaveLength(2)
  })
})
