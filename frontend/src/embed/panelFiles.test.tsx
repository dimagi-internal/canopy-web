// @vitest-environment jsdom
// The panel's history: its sealed files read through the frame's own client, shown as
// "Attachment" until the key opens, then opened (name, preview) in the frame.
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { sealedAttachmentHost } from '@/components/chat/SealedAttachments'
import { sealAttachment } from '@/vault/sealedFiles'
import { generateKeyPair } from '@/vault/zk'
import { formDataSafeFetch, panelSealedFilesApi } from './panelFiles'

const line = { id: 't4', turn_index: 4, role: 'user' as const, content: {}, plaintext: 'see this', status: 'complete' as const,
  error_detail: null, started_at: null, completed_at: null, created_at: '' }

describe('the panel\'s sealed files', () => {
  it('lists and fetches through the frame\'s client, and opens the file in the frame', async () => {
    const key = await generateKeyPair()
    const ck = { conversation: 'conv-1', privateKey: key.privateKey, publicJwk: key.publicJwk, scalar: key.scalar }
    const { sealedName, blob } = await sealAttachment({ conversation: 'conv-1', public_jwk: key.publicJwk },
      { id: 'a-1', uploadedBy: 7 }, { name: 'amina.png', type: 'image/png', bytes: new Uint8Array([1, 2, 3]) })
    const row = { id: 'a-1', turn_index: 4, uploaded_by: 7, family: 'image/*', size_bucket: 65536, sealed_name: sealedName }
    const rest = {
      json: vi.fn(async () => [row]),
      raw: vi.fn(async () => new Response(blob as Uint8Array<ArrayBuffer>, { status: 200 })),
    }
    const api = panelSealedFilesApi(rest as never)
    expect(await api.list('sess-1')).toEqual([row])
    expect(rest.json).toHaveBeenCalledWith('/api/canopy-sessions/sess-1/sealed-attachments')

    const locked = render(<>{sealedAttachmentHost([row], null, () => undefined, api)!.render(line)}</>)
    expect(screen.getByTestId('sealed-attachment-locked').textContent).toContain('Attachment')
    expect(document.body.textContent).not.toContain('amina')
    locked.unmount()

    URL.createObjectURL = vi.fn(() => 'blob:x')
    URL.revokeObjectURL = vi.fn()
    render(<>{sealedAttachmentHost([row], ck, () => undefined, api)!.render(line)}</>)
    await waitFor(() => expect(screen.getByText('amina.png')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Preview amina.png/ }))
    await waitFor(() => expect(screen.getByRole('img', { name: 'amina.png' })).toBeTruthy())
    expect(rest.raw).toHaveBeenCalledWith('/api/canopy-sessions/attachments/a-1/sealed')
  })

  it('lets the browser label a multipart body, and leaves JSON as it is', async () => {
    const seen: RequestInit[] = []
    vi.stubGlobal('fetch', vi.fn(async (_u: unknown, init: RequestInit) => { seen.push(init); return new Response('{}') }))
    await formDataSafeFetch('/x', { method: 'POST', body: new FormData(),
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer t' } })
    await formDataSafeFetch('/y', { method: 'POST', body: '{}', headers: { 'Content-Type': 'application/json' } })
    expect(seen[0].headers).toEqual({ Authorization: 'Bearer t' })
    expect(seen[1].headers).toEqual({ 'Content-Type': 'application/json' })
    vi.unstubAllGlobals()
  })
})
