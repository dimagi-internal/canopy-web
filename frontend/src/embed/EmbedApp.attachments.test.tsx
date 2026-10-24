// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { generateKeyPair } from '@/vault/zk'
import { openSealedBlob, openSealedName } from '@/vault/sealedFiles'
import type { ActionSpec, HostInit, HostLink } from './hostLink'

/**
 * A file in a private conversation, attached from the embedded panel: the frame uploads
 * it for the agent's one turn through its own delegated token, then seals the people's
 * copy HERE to the conversation's key and stores that — multipart, with the browser's own
 * Content-Type. The panel's history reads the sealed files through the same token.
 * (Who the server lets do this — the frame, never the site's backend — is
 * tests/test_private_attachments.py.)
 */

const { EmbedApp } = await import('./EmbedApp')

const AGENTS = [{ slug: 'hal', name: 'Hal', description: '', avatar_url: '', workspace: 'connect' }]

function fakeLink(): HostLink {
  const init: HostInit = { token: 't', agent: 'hal', actions: [] }
  return {
    waitForInit: async () => init,
    requestToken: async () => ({ token: 'tok', expiresAt: '' }),
    requestContext: async () => ({ route: '/' }),
    runAction: async () => undefined,
    actions: (): ActionSpec[] => [],
    onActionsChanged: () => () => undefined,
    pageState: () => null,
    onPageStateChanged: () => () => undefined,
    onThemeChanged: () => () => undefined,
    onAsk: () => () => undefined,
    onOpenSession: () => () => undefined,
    reportSession: () => undefined,
    invalidate: () => undefined,
    requestClose: () => undefined,
    requestHeight: () => undefined,
    requestExpand: () => undefined,
    isExpanded: () => false,
    onDisplayChanged: () => () => undefined,
    requestSnap: () => undefined,
    isSnapped: () => false,
    onSnapChanged: () => () => undefined,
    dispose: () => undefined,
  }
}

let calls: Array<{ url: string; init?: RequestInit }>

function serve(session: Record<string, unknown>) {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init })
    const path = String(url)
    const ok = (body: unknown, status = 200) => ({ ok: true, status, json: async () => body }) as Response
    if (/\/canopy-sessions\/sess-1$/.test(path) && (!init?.method || init.method === 'GET')) return ok(session)
    if (path.includes('/api/embed/agents')) return ok(AGENTS)
    if (/\/sess-1\/attachments$/.test(path)) {
      return ok({ id: 'a-1', filename: 'amina.png', content_type: 'image/*', size_bytes: 65536 }, 201)
    }
    if (/\/attachments\/a-1\/sealed$/.test(path)) return ok(undefined, 204)
    if (/\/sess-1\/sealed-attachments$/.test(path)) return ok([])
    if (/\/sess-1\/join-requests$/.test(path)) return ok([])
    return ok({ id: 'sess-1' })
  }))
}

beforeEach(() => {
  calls = []
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('a file in a private conversation, from the embedded panel', () => {
  it('uploads it for the turn, then seals the people\'s copy in the frame', async () => {
    const key = await generateKeyPair()
    serve({ id: 'sess-1', sensitive: true, my_role: 'owner', sealed_key: {
      conversation: 'conv-1', public_jwk: key.publicJwk, current: true,
      wrap: { alg: 'ecdh-es', epk: 'e', n: 'n', ct: 'c' },
      keyset: { user_id: '7', prf_input: 'p', public_jwk: {}, seals: [] } } })
    render(<EmbedApp link={fakeLink()} app="canopy-web" />)
    const box = await screen.findByPlaceholderText(/^Type a message/)
    fireEvent.change(box, { target: { value: 'hello' } })
    fireEvent.click(screen.getByTestId('send'))
    const input = await screen.findByTestId('attachment-input')
    // Waits for the panel to learn the conversation is private (it refuses until then).
    await waitFor(() => expect(calls.some((c) => /\/canopy-sessions\/sess-1$/.test(c.url))).toBe(true))
    await waitFor(() => expect(calls.some((c) => /\/sess-1\/sealed-attachments$/.test(c.url))).toBe(true))
    const file = new File(['amina-pixels'], 'amina.png', { type: 'image/png' })
    fireEvent.change(input, { target: { files: [file] } })

    await waitFor(() => expect(calls.some((c) => /\/attachments\/a-1\/sealed$/.test(c.url))).toBe(true))
    const upload = calls.find((c) => /\/sess-1\/attachments$/.test(c.url))!
    const sealed = calls.find((c) => /\/attachments\/a-1\/sealed$/.test(c.url))!
    for (const c of [upload, sealed]) {
      const headers = c.init!.headers as Record<string, string>
      expect(headers.Authorization).toMatch(/^Bearer /)
      expect(headers['Content-Type']).toBeUndefined()  // the browser writes the multipart boundary
      expect(c.init!.body).toBeInstanceOf(FormData)
    }
    const form = sealed.init!.body as FormData
    const blob = new Uint8Array(await (form.get('file') as Blob).arrayBuffer())
    const ck = { conversation: 'conv-1', privateKey: key.privateKey, publicJwk: key.publicJwk, scalar: key.scalar }
    const meta = await openSealedName(String(form.get('sealed_name')), ck, { id: 'a-1', uploadedBy: '7' })
    expect(meta.name).toBe('amina.png')
    expect(new TextDecoder().decode(await openSealedBlob(blob, ck, { id: 'a-1', uploadedBy: '7' }, meta)))
      .toBe('amina-pixels')
    // The sealed bytes and envelope carry no name in clear.
    expect(new TextDecoder('latin1').decode(blob) + String(form.get('sealed_name'))).not.toContain('amina')
  })
})
