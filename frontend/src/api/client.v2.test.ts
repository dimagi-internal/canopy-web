import { describe, it, expect } from 'vitest'
import { isLoginBounceInFlight, rewriteForWorkspace, shouldBounceToLogin } from './client.v2'

// The login-loop breaker. A lapsed session should bounce through OAuth exactly
// once; a second 401 landing back inside the window means the round-trip isn't
// sticking (rejected cookie, IdP hiccup, a service worker shadowing /accounts/ —
// the #244 outage), so we must HOLD and surface the login screen instead of
// navigating again. This is the pure decision behind that guard; the DOM/
// sessionStorage plumbing around it is not exercised here (this file's "no live
// DOM" convention), but the branch that actually prevents the infinite loop is.
describe('shouldBounceToLogin', () => {
  const WINDOW = 10_000

  it('bounces when the session lapses and we have never redirected', () => {
    expect(shouldBounceToLogin(0, 1_000_000)).toBe(true)
  })

  it('holds — breaks the loop — on a second 401 within the window', () => {
    const now = 1_000_000
    expect(shouldBounceToLogin(now - 500, now)).toBe(false)
  })

  it('bounces again once the window has elapsed (a genuine later expiry)', () => {
    const now = 1_000_000
    expect(shouldBounceToLogin(now - (WINDOW + 1), now)).toBe(true)
  })

  it('treats the exact window boundary as elapsed, so it bounces', () => {
    const now = 1_000_000
    expect(shouldBounceToLogin(now - WINDOW, now)).toBe(true)
  })
})

// The flicker guard, and specifically why it is NOT the loop guard's stamp.
//
// Both answer a question about bouncing, but different ones, and conflating
// them reintroduces the bug in the worst case. `shouldBounceToLogin` reads a
// sessionStorage stamp that deliberately OUTLIVES the page — it has to survive
// the navigation to Google and back, so it is still "recent" when we return
// from a FAILED round-trip. That is precisely the moment the login card must
// render, because the guard has declined to navigate and only the user can
// drive it now.
//
// `isLoginBounceInFlight` asks the narrower question the UI actually needs:
// have we committed to a navigation in THIS page load? A full-page navigation
// ends the page, so the answer resets by construction — no window, no clock.
describe('isLoginBounceInFlight', () => {
  it('is false on a fresh page load, so the login card renders by default', () => {
    // No 401 has been handled in this module instance. The card is the correct
    // default: hiding it behind a bounce that never happens would leave an
    // anonymous user staring at "Signing in…" forever.
    expect(isLoginBounceInFlight()).toBe(false)
  })
})

// Regression test for the workspace-rewrite middleware eating request bodies.
//
// The middleware used to rebuild the rewritten request via
// `new Request(url, request)`. That constructor form adopts the source
// Request's body as a *live stream*, which consumes/disturbs it — so every
// workspace-scoped POST/PATCH/PUT died at the network layer with
// "TypeError: Failed to fetch" before it ever left the browser (no response,
// no status). A test that only checks the rewritten URL would NOT have
// caught this — the URL rewrite was always correct; only the body died.
describe('rewriteForWorkspace', () => {
  it('preserves a JSON POST body while rewriting onto the tenant path', async () => {
    const payload = { title: 'accept', note: 'flip to accepted' }
    const original = new Request('http://localhost/api/agents/echo/tasks/1/commands', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })

    const rewritten = await rewriteForWorkspace(original, 'acme')

    // URL rewrite: /api/<app>/… -> /api/w/:ws/<app>/…
    expect(new URL(rewritten.url).pathname).toBe(
      '/api/w/acme/agents/echo/tasks/1/commands',
    )
    expect(rewritten.method).toBe('POST')

    // The assertion that actually matters: the body must have survived the
    // rewrite. Against the bug, this throws/reads empty because the source
    // stream was already disturbed by `new Request(url, request)`.
    await expect(rewritten.json()).resolves.toEqual(payload)
  })

  it('rewrites a harness path — the explicit-workspace (header) case is not limited to the implicit prefix list', async () => {
    // A repo turn is dispatched from /supervisor (not a tenant surface) and pins
    // its workspace via WORKSPACE_HEADER; the middleware then rewrites via this
    // function regardless of whether /api/harness is in WS_SCOPED_API_PREFIXES
    // (it is not — that list is only for the implicit URL-driven case).
    const original = new Request('http://localhost/api/harness/turns/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: 'canopy-web', prompt: 'fix the header' }),
    })

    const rewritten = await rewriteForWorkspace(original, 'dimagi')

    expect(new URL(rewritten.url).pathname).toBe('/api/w/dimagi/harness/turns/')
    await expect(rewritten.json()).resolves.toEqual({ project: 'canopy-web', prompt: 'fix the header' })
  })

  it('rewrites a bodyless GET without attaching a body', async () => {
    const original = new Request('http://localhost/api/projects/', { method: 'GET' })

    const rewritten = await rewriteForWorkspace(original, 'acme')

    expect(new URL(rewritten.url).pathname).toBe('/api/w/acme/projects/')
    expect(rewritten.method).toBe('GET')
    expect(rewritten.body).toBeNull()
  })

  it('rewrites a nested matched path without an off-by-one in the prefix slice', async () => {
    // rewriteForWorkspace itself doesn't prefix-match (that's done by the
    // caller in onRequest); this test just documents the pure rewrite math
    // for a nested path, guarding against an off-by-one in the slice().
    const original = new Request('http://localhost/api/walkthroughs/abc123/', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ visibility: 'link' }),
    })

    const rewritten = await rewriteForWorkspace(original, 'dimagi')

    expect(new URL(rewritten.url).pathname).toBe('/api/w/dimagi/walkthroughs/abc123/')
    await expect(rewritten.json()).resolves.toEqual({ visibility: 'link' })
  })

  // Node's fetch/Request implementation tolerates the literal buggy pattern
  // (`new Request(url, request)`) in a way Chrome does not — verified by hand
  // against a local HTTP server, the two-arg form neither throws nor drops
  // the body under Node. So the black-box assertions above, on their own,
  // would NOT fail if this function regressed back to that exact line — only
  // Playwright (real Chromium) reproduces the browser-only "Failed to fetch"
  // network-layer failure. This white-box check closes that gap for CI: it
  // asserts on `original.bodyUsed`, which flips false -> true the moment the
  // body is read via ANY buffering method (arrayBuffer/text/json/blob) — so
  // it discriminates against the bug (which leaves it false, handing the
  // constructor a live, unread stream) without pinning which specific method
  // the implementation uses to do the reading. Caveat: a `.clone()`-based fix
  // wouldn't touch `original.bodyUsed` at all (the clone's body is read
  // instead), so this assertion — like the ones above — would not catch a
  // regression to that pattern either. It only guards the "hand the
  // constructor a live stream" failure mode this test exists for.
  it('reads the source body eagerly instead of handing the constructor a live stream', async () => {
    const original = new Request('http://localhost/api/agents/echo/tasks/1/commands', {
      method: 'POST',
      body: JSON.stringify({ ok: true }),
    })

    expect(original.bodyUsed).toBe(false)

    await rewriteForWorkspace(original, 'acme')

    expect(original.bodyUsed).toBe(true)
  })
})
