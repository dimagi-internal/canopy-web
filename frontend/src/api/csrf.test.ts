// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { getCsrfToken, rememberCsrfToken } from './base'
import { bootstrapCsrf } from './csrf'

// Inside the browser extension's side panel Chrome sends canopy's cookies but hides them from
// `document.cookie`: the token /api/csrf/ says is then the only one a write can carry.
describe('the CSRF token a page that cannot read its cookies sends', () => {
  afterEach(() => {
    rememberCsrfToken('')
    vi.unstubAllGlobals()
  })

  it('is the one /api/csrf/ said when the cookie reads empty', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ok: true, token: 'told-token' }))))
    await bootstrapCsrf()
    expect(getCsrfToken()).toBe('told-token')
  })

  it('keeps working against a body with no token (an older server)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ok: true }))))
    await bootstrapCsrf()
    expect(getCsrfToken()).toBe('')
  })
})
