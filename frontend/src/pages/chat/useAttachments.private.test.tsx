// @vitest-environment jsdom
// A private conversation's file is two uploads: the file for the agent's one turn, then
// the people's copy, sealed in this browser to the conversation's key. The chip is not
// ready to send until both are stored, and a file whose sealed copy did not land is
// discarded. With no key to seal to, the file is refused and nothing is uploaded.
import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

import { generateKeyPair } from '@/vault/zk'
import { openSealedBlob, openSealedName } from '@/vault/sealedFiles'

const uploadAttachment = vi.fn()
const uploadSealedCopy = vi.fn()
const deleteAttachment = vi.fn(() => Promise.resolve())
vi.mock('@/api/chat', () => ({
  uploadAttachment: (...a: unknown[]) => uploadAttachment(...a),
  uploadSealedCopy: (...a: unknown[]) => uploadSealedCopy(...a),
  deleteAttachment: (...a: unknown[]) => deleteAttachment(...(a as [])),
}))

const { PRIVATE_NO_KEY, useAttachments } = await import('./useAttachments')

beforeEach(() => {
  uploadAttachment.mockReset()
  uploadSealedCopy.mockReset()
  deleteAttachment.mockClear()
})

describe('useAttachments in a private conversation', () => {
  it('uploads the file, seals the people\'s copy here, and is ready only once both are stored', async () => {
    const key = await generateKeyPair()
    uploadAttachment.mockResolvedValue({ id: 'a-1', filename: 'amina.png', content_type: 'image/*', size_bytes: 65536 })
    let release: () => void = () => undefined
    uploadSealedCopy.mockImplementation(() => new Promise<void>((r) => { release = r }))
    const privateFiles = { target: { conversation: 'conv-1', public_jwk: key.publicJwk }, userId: 7 }
    const { result } = renderHook(() => useAttachments('s1', undefined, privateFiles))
    const file = new File(['amina-pixels'], 'amina.png', { type: 'image/png' })
    act(() => result.current.attach([file]))
    await waitFor(() => expect(uploadSealedCopy).toHaveBeenCalled())
    // Still uploading while the sealed copy is on its way: the composer will not send.
    expect(result.current.attachments[0]).toEqual(expect.objectContaining({ uploading: true }))
    const [aid, sealedName, blob] = uploadSealedCopy.mock.calls[0] as [string, string, Uint8Array]
    expect(aid).toBe('a-1')
    // What was sent opens with the conversation key, bound to this file and uploader.
    const ck = { conversation: 'conv-1', privateKey: key.privateKey, publicJwk: key.publicJwk, scalar: key.scalar }
    const meta = await openSealedName(sealedName, ck, { id: 'a-1', uploadedBy: 7 })
    expect(meta.name).toBe('amina.png')
    expect(new TextDecoder().decode(await openSealedBlob(blob, ck, { id: 'a-1', uploadedBy: 7 }, meta))).toBe('amina-pixels')
    await act(async () => release())
    await waitFor(() => expect(result.current.attachments[0]).toEqual({ id: 'a-1', filename: 'amina.png' }))
  })

  it('discards a file whose sealed copy did not land', async () => {
    const key = await generateKeyPair()
    uploadAttachment.mockResolvedValue({ id: 'a-2', filename: 'x.png', content_type: 'image/*', size_bytes: 1 })
    uploadSealedCopy.mockRejectedValue(new Error('sealing failed (422)'))
    const privateFiles = { target: { conversation: 'conv-1', public_jwk: key.publicJwk }, userId: 7 }
    const { result } = renderHook(() => useAttachments('s1', undefined, privateFiles))
    act(() => result.current.attach([new File(['x'], 'x.png', { type: 'image/png' })]))
    await waitFor(() => expect(result.current.attachments[0]).toEqual(
      expect.objectContaining({ uploading: false, error: 'sealing failed (422)' })))
    expect(deleteAttachment).toHaveBeenCalledWith('a-2')
  })

  it('refuses the file in the chip and uploads nothing when there is no key to seal to', () => {
    const { result } = renderHook(() => useAttachments('s1', PRIVATE_NO_KEY))
    act(() => result.current.attach([new File(['x'], 'amina.png', { type: 'image/png' })]))
    expect(result.current.attachments).toEqual([
      expect.objectContaining({ filename: 'amina.png', uploading: false, error: PRIVATE_NO_KEY })])
    expect(uploadAttachment).not.toHaveBeenCalled()
  })
})
