// A private conversation's file, sealed in the browser and opened by its people
// (vault/sealedFiles.ts). The vector is made by the backend suite standing in for this
// construction (`tests/test_private_attachments.py::_vector`), so both languages agree.
import { describe, expect, it } from 'vitest'
import raw from '../../../docs/vault/chat-v1/file_v1_vectors.json?raw'
import type { ConversationKey } from './sealedChat'
import { NAME_PREFIX, openSealedBlob, openSealedName, paddedLength, sealAttachment } from './sealedFiles'
import { generateKeyPair, hexToBytes, importPrivateScalar, publicJwk, unb64u } from './zk'

const V = JSON.parse(raw) as {
  inputs: { conversation: string; attachment: string; uploader: string; conversation_priv_scalar_hex: string
            conversation_pub_jwk: { x: string; y: string }; meta: { name: string; type: string; size: number }
            bytes_b64u: string }
  outputs: { sealed_name: string; blob_b64u: string }
}
const I = V.inputs

async function vectorKey(): Promise<ConversationKey> {
  const scalar = hexToBytes(I.conversation_priv_scalar_hex)
  const pub = publicJwk(I.conversation_pub_jwk)
  return { conversation: I.conversation, privateKey: await importPrivateScalar(scalar, pub), publicJwk: pub, scalar }
}

async function freshKey(conversation = 'c-1'): Promise<ConversationKey> {
  const { privateKey, publicJwk: pub, scalar } = await generateKeyPair()
  return { conversation, privateKey, publicJwk: pub, scalar }
}

describe('sealedFiles', () => {
  it('opens the name and bytes the vector sealed', async () => {
    const key = await vectorKey()
    const at = { id: I.attachment, uploadedBy: I.uploader }
    const meta = await openSealedName(V.outputs.sealed_name, key, at)
    expect(meta).toEqual(I.meta)
    const bytes = await openSealedBlob(unb64u(V.outputs.blob_b64u), key, at, meta)
    expect(Array.from(bytes)).toEqual(Array.from(unb64u(I.bytes_b64u)))
  })

  it('seals a file its people open again, and the sealed copy carries neither the name nor the bytes', async () => {
    const key = await freshKey()
    const bytes = new TextEncoder().encode('amina-pixels'.repeat(500))
    const { sealedName, blob } = await sealAttachment({ conversation: key.conversation, public_jwk: key.publicJwk },
      { id: 'a-1', uploadedBy: 7 }, { name: 'amina-results.png', type: 'image/png', bytes })
    expect(sealedName.startsWith(NAME_PREFIX)).toBe(true)
    const text = new TextDecoder('latin1').decode(blob) + sealedName
    expect(text).not.toContain('amina')
    expect(blob.length).toBeGreaterThanOrEqual(paddedLength(bytes.length))
    const meta = await openSealedName(sealedName, key, { id: 'a-1', uploadedBy: 7 })
    expect(meta).toEqual({ name: 'amina-results.png', type: 'image/png', size: bytes.length })
    const opened = await openSealedBlob(blob, key, { id: 'a-1', uploadedBy: 7 }, meta)
    expect(new TextDecoder().decode(opened)).toBe('amina-pixels'.repeat(500))
  })

  it('opens a file only where it was sealed: this conversation, this attachment, this uploader', async () => {
    const key = await vectorKey()
    const at = { id: I.attachment, uploadedBy: I.uploader }
    await expect(openSealedName(V.outputs.sealed_name, key, { ...at, id: 'another' })).rejects.toThrow(/another attachment/)
    await expect(openSealedName(V.outputs.sealed_name, key, { ...at, uploadedBy: '8' })).rejects.toThrow(/someone else/)
    await expect(openSealedName(V.outputs.sealed_name, { ...key, conversation: 'other' }, at)).rejects.toThrow(/another conversation/)
    // An altered byte does not open.
    const blob = unb64u(V.outputs.blob_b64u)
    blob[blob.length - 1] ^= 1
    await expect(openSealedBlob(blob, key, at, I.meta)).rejects.toThrow()
    // Nor with someone else's key.
    await expect(openSealedName(V.outputs.sealed_name, { ...(await freshKey(I.conversation)) }, at)).rejects.toThrow()
  })

  it('pads to a little over the file, never more than an eighth', () => {
    expect(paddedLength(1)).toBe(4096)
    for (const n of [4097, 70_000, 1_000_001, 10 * 1024 * 1024]) {
      const p = paddedLength(n)
      expect(p).toBeGreaterThanOrEqual(n)
      expect(p).toBeLessThanOrEqual(n + n / 8)
    }
  })
})
