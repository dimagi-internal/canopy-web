// @vitest-environment jsdom
// A private conversation's sent files, under the line they went with: "Attachment"
// until the conversation key opens, then the real name, a preview and a download — all
// opened here, from ciphertext.
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { generateKeyPair } from '@/vault/zk'
import { sealAttachment } from '@/vault/sealedFiles'

const fetchSealedAttachment = vi.fn()
vi.mock('@/api/chat', () => ({
  fetchSealedAttachment: (...a: unknown[]) => fetchSealedAttachment(...a),
  listSealedAttachments: vi.fn(),
}))

const { LOCKED_FILE, SealedAttachments, sealedAttachmentHost } = await import('./SealedAttachments')

async function sealed() {
  const key = await generateKeyPair()
  const ck = { conversation: 'conv-1', privateKey: key.privateKey, publicJwk: key.publicJwk, scalar: key.scalar }
  const bytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3])
  const { sealedName, blob } = await sealAttachment({ conversation: 'conv-1', public_jwk: key.publicJwk },
    { id: 'a-1', uploadedBy: 7 }, { name: 'amina-results.png', type: 'image/png', bytes })
  const row = { id: 'a-1', turn_index: 4, uploaded_by: 7, family: 'image/*', size_bucket: 65536, sealed_name: sealedName }
  return { ck, row, blob, bytes }
}

describe('SealedAttachments', () => {
  it('reads "Attachment" until the key opens, and asks for the unlock on a click', async () => {
    const { row } = await sealed()
    const onUnlock = vi.fn()
    render(<SealedAttachments files={[row]} conversationKey={null} onUnlock={onUnlock} />)
    const chip = screen.getByTestId('sealed-attachment-locked')
    expect(chip.textContent).toContain(LOCKED_FILE)
    expect(document.body.textContent).not.toContain('amina')
    fireEvent.click(chip)
    expect(onUnlock).toHaveBeenCalled()
  })

  it('shows the real name once the key opens, and previews the file it decrypts here', async () => {
    const { ck, row, blob } = await sealed()
    fetchSealedAttachment.mockResolvedValue(blob)
    const created: Blob[] = []
    URL.createObjectURL = vi.fn((b: Blob) => { created.push(b); return 'blob:preview' })
    URL.revokeObjectURL = vi.fn()
    render(<SealedAttachments files={[row]} conversationKey={ck} onUnlock={() => undefined} />)
    await waitFor(() => expect(screen.getByText('amina-results.png')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Preview amina-results.png/ }))
    await waitFor(() => expect(screen.getByRole('img', { name: 'amina-results.png' })).toBeTruthy())
    expect(fetchSealedAttachment).toHaveBeenCalledWith('a-1')
    expect(created[0].type).toBe('image/png')
    expect(new Uint8Array(await created[0].arrayBuffer())).toEqual(new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3]))
  })

  it('places each file under the line it was sent with', async () => {
    const { row } = await sealed()
    const host = sealedAttachmentHost([row], null, () => undefined)!
    const line = (turn_index: number) => ({ id: 't', turn_index, role: 'user' as const, content: {}, plaintext: '',
      status: 'complete' as const, error_detail: null, started_at: null, completed_at: null, created_at: '' })
    expect(host.render(line(3))).toBeNull()
    expect(host.render(line(4))).not.toBeNull()
    expect(sealedAttachmentHost([], null, () => undefined)).toBeUndefined()
  })
})
